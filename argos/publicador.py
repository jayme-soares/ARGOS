"""Snapshots que alimentam o painel web, um por contrato.

Cada monitor atualiza a sua seção ("programaveis" / "campo") e o status no
snapshot do contrato; a cada atualização o snapshot inteiro é gravado em
DATA_DIR/<subpasta do contrato>snapshot.json e publicado no Upstash Redis,
de onde as funções web/api/snapshot.js e tv.js leem (via web/api/_snapshot.js).
A VPS não precisa expor porta nenhuma.

No Upstash o snapshot fica em duas chaves, as duas em gzip+base64 ("gz:..."),
para caber na banda do plano gratuito (10 GB/mês):
  <snapshot_key>        tudo menos o campo, com "campo_hash" (muda a cada minuto, ~3 KB)
  <snapshot_key>:campo  "<hash>:gz:..." com a seção campo (~250 KB na Negociação)
O campo só muda a cada exportação (30 min), então só é regravado quando muda;
antes o snapshot inteiro (2 MB) ia a cada minuto e estourou a banda.

As mesmas chaves vão também para a tabela argos_snapshot do Supabase
(migração 007), como reserva: se o Upstash falhar, o painel lê de lá. Cada
destino falha sozinho, sem atrapalhar o outro.

A cópia local também serve para, depois de um restart, o painel não ficar
vazio até a próxima exportação de campo (que pode levar até 30 min).
"""

import base64
import copy
import gzip
import hashlib
import json
import threading
from datetime import datetime

import requests

from argos import config, webpush
from argos.contratos import SOC, TODOS, Contrato
from argos.estado import carregar_json, salvar_json
from argos.log import log

ARQUIVO_SNAPSHOT = "snapshot.json"

_lock = threading.Lock()


def _snapshot_vazio(contrato: Contrato) -> dict:
    return {
        "versao": 1,
        "contrato": {"id": contrato.id, "nome": contrato.nome},
        "gerado_em": None,
        "janela": {
            "inicio": config.HORARIO_INICIO,
            "fim": config.HORARIO_FIM,
            "dias_semana": sorted(config.DIAS_SEMANA),
        },
        "config": {
            "intervalo_campo_min": config.INTERVALO_CAMPO_MINUTOS,
            "intervalo_programaveis_seg": config.INTERVALO_PROGRAMAVEIS_SEGUNDOS,
            "alertas_min": config.ALERTAS_MINUTOS,
            # O painel usa para inscrever o navegador no Web Push; None = desativado.
            "push_chave_publica": webpush.chave_publica() if webpush.habilitado() else None,
        },
        "status": {},
        "programaveis": {"atualizado_em": None, "total": 0, "registros": []},
        "campo": {"atualizado_em": None, "arquivo": None, "registros": [], "finalizadas": []},
    }


_snapshots = {c.id: _snapshot_vazio(c) for c in TODOS}
# Por destino ("Upstash" / "Supabase") e contrato: último erro (para logar só
# quando muda) e hash do campo já gravado (ausente = ainda não gravado nesta
# execução, então a primeira publicação depois de um restart regrava).
_ultimo_erro: dict[tuple[str, str], str | None] = {}
_campo_publicado: dict[tuple[str, str], str] = {}


def _comprimir(texto: str) -> str:
    return "gz:" + base64.b64encode(gzip.compress(texto.encode("utf-8"), 6)).decode("ascii")


def chave_campo(contrato: Contrato) -> str:
    return f"{contrato.snapshot_key}:campo"


def agora_iso() -> str:
    return datetime.now(config.TIMEZONE).isoformat(timespec="seconds")


def carregar_snapshot_local():
    """Recupera as seções de dados salvas antes de um restart."""
    for contrato in TODOS:
        salvo = carregar_json(contrato.arquivo(ARQUIVO_SNAPSHOT))
        if not salvo:
            continue
        with _lock:
            for secao in ("programaveis", "campo"):
                if isinstance(salvo.get(secao), dict):
                    _snapshots[contrato.id][secao] = salvo[secao]


def obter_secao(secao: str, contrato: Contrato = SOC):
    with _lock:
        return copy.deepcopy(_snapshots[contrato.id].get(secao))


def obter_status():
    """{contrato: {monitor: status}} para o health-check."""
    with _lock:
        return {cid: copy.deepcopy(s["status"]) for cid, s in _snapshots.items() if s["status"]}


def atualizar_secao(secao: str, dados: dict, contrato: Contrato = SOC):
    with _lock:
        _snapshots[contrato.id][secao] = dados
    publicar(contrato)


def atualizar_status(chave: str, estado: str, contratos: tuple[Contrato, ...] = (SOC,), publicar_agora: bool = True):
    """`chave` é o nome do monitor no painel ("programaveis" / "campo"). O
    monitor de campo é um só para os dois contratos e aparece nos dois.
    Só republica quando o estado muda — o painel não precisa de uma
    escrita no Redis a cada batida do watchdog."""
    mudou = []
    with _lock:
        for contrato in contratos:
            status = _snapshots[contrato.id]["status"]
            anterior = status.get(chave, {}).get("estado")
            status[chave] = {"estado": estado, "atualizado_em": agora_iso()}
            if anterior != estado:
                mudou.append(contrato)
    if publicar_agora:
        for contrato in mudou:
            publicar(contrato)


def publicar(contrato: Contrato = SOC):
    with _lock:
        snapshot = _snapshots[contrato.id]
        snapshot["gerado_em"] = agora_iso()
        dados = copy.deepcopy(snapshot)

    try:
        salvar_json(contrato.arquivo(ARQUIVO_SNAPSHOT), dados)
    except Exception as e:
        log(f"Falha ao salvar snapshot local ({contrato.nome}): {e}", "SNAPSHOT")

    campo_json = json.dumps(dados.pop("campo"), ensure_ascii=False, sort_keys=True)
    campo_hash = hashlib.sha1(campo_json.encode("utf-8")).hexdigest()[:16]
    dados["campo_hash"] = campo_hash
    principal = _comprimir(json.dumps(dados, ensure_ascii=False))
    campo = None  # comprimido só se algum destino precisar

    for destino, gravar in _destinos():
        itens = {}
        # O campo vai antes: quem ler a chave principal já com o hash novo
        # encontra o campo novo.
        if _campo_publicado.get((destino, contrato.id)) != campo_hash:
            campo = campo or f"{campo_hash}:{_comprimir(campo_json)}"
            itens[chave_campo(contrato)] = campo
        itens[contrato.snapshot_key] = principal
        chave = (destino, contrato.id)
        try:
            gravar(itens)
            _campo_publicado[chave] = campo_hash
            if _ultimo_erro.get(chave):
                log(f"Publicação no {destino} ({contrato.nome}) voltou a funcionar.", "SNAPSHOT")
            _ultimo_erro[chave] = None
        except Exception as e:
            # Loga só quando o erro muda, para não poluir o log a cada minuto
            # enquanto o destino estiver fora.
            msg = str(e)
            if msg != _ultimo_erro.get(chave):
                log(f"Falha ao publicar snapshot ({contrato.nome}) no {destino}: {msg}", "SNAPSHOT")
            _ultimo_erro[chave] = msg


def _destinos():
    destinos = []
    if config.UPSTASH_REDIS_REST_URL and config.UPSTASH_REDIS_REST_TOKEN:
        destinos.append(("Upstash", _gravar_upstash))
    if config.SUPABASE_URL and config.SUPABASE_ANON_KEY and config.PUSH_CHAVE:
        destinos.append(("Supabase", _gravar_supabase))
    return destinos


def _gravar_upstash(itens: dict[str, str]):
    # API REST do Upstash: /pipeline recebe uma lista de comandos Redis.
    resp = requests.post(
        f"{config.UPSTASH_REDIS_REST_URL.rstrip('/')}/pipeline",
        headers={"Authorization": f"Bearer {config.UPSTASH_REDIS_REST_TOKEN}"},
        json=[["SET", k, v] for k, v in itens.items()],
        timeout=15,
    )
    resp.raise_for_status()
    erros = [r["error"] for r in resp.json() if isinstance(r, dict) and r.get("error")]
    if erros:
        raise RuntimeError("; ".join(erros))


def _gravar_supabase(itens: dict[str, str]):
    resp = requests.post(
        f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/rpc/argos_snapshot_gravar",
        headers={"apikey": config.SUPABASE_ANON_KEY, "Authorization": f"Bearer {config.SUPABASE_ANON_KEY}"},
        json={"p_chave": config.PUSH_CHAVE, "p_itens": itens},
        timeout=15,
    )
    if not resp.ok:
        raise RuntimeError(f"{resp.status_code}: {resp.text[:200]}")
