"""Snapshot único que alimenta o painel web.

Cada monitor atualiza a sua seção ("programaveis" / "campo") e o status; a
cada atualização o snapshot inteiro é gravado em DATA_DIR/snapshot.json e
publicado no Upstash Redis (chave SNAPSHOT_KEY), de onde a função
web/api/snapshot.js lê. A VPS não precisa expor porta nenhuma.

A cópia local também serve para, depois de um restart, o painel não ficar
vazio até a próxima exportação de campo (que pode levar até 30 min).
"""

import copy
import json
import threading
from datetime import datetime

import requests

from argos import config, webpush
from argos.estado import carregar_json, salvar_json
from argos.log import log

ARQUIVO_SNAPSHOT = "snapshot.json"

_lock = threading.Lock()
_snapshot = {
    "versao": 1,
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
    "campo": {"atualizado_em": None, "arquivo": None, "registros": []},
}
_ultimo_erro_upstash = None


def agora_iso() -> str:
    return datetime.now(config.TIMEZONE).isoformat(timespec="seconds")


def carregar_snapshot_local():
    """Recupera as seções de dados salvas antes de um restart."""
    salvo = carregar_json(ARQUIVO_SNAPSHOT)
    if not salvo:
        return
    with _lock:
        for secao in ("programaveis", "campo"):
            if isinstance(salvo.get(secao), dict):
                _snapshot[secao] = salvo[secao]


def obter_secao(secao: str):
    with _lock:
        return copy.deepcopy(_snapshot.get(secao))


def obter_status():
    with _lock:
        return copy.deepcopy(_snapshot["status"])


def atualizar_secao(secao: str, dados: dict):
    with _lock:
        _snapshot[secao] = dados
    publicar()


def atualizar_status(worker: str, estado: str, publicar_agora: bool = True):
    """Só republica quando o estado muda — o painel não precisa de uma
    escrita no Redis a cada batida do watchdog."""
    with _lock:
        anterior = _snapshot["status"].get(worker, {}).get("estado")
        _snapshot["status"][worker] = {"estado": estado, "atualizado_em": agora_iso()}
    if publicar_agora and anterior != estado:
        publicar()


def publicar():
    global _ultimo_erro_upstash
    with _lock:
        _snapshot["gerado_em"] = agora_iso()
        corpo = json.dumps(_snapshot, ensure_ascii=False)
        dados = copy.deepcopy(_snapshot)

    try:
        salvar_json(ARQUIVO_SNAPSHOT, dados)
    except Exception as e:
        log(f"Falha ao salvar snapshot local: {e}", "SNAPSHOT")

    if not (config.UPSTASH_REDIS_REST_URL and config.UPSTASH_REDIS_REST_TOKEN):
        return
    try:
        # API REST do Upstash: o corpo é o comando Redis como array JSON.
        resp = requests.post(
            config.UPSTASH_REDIS_REST_URL,
            headers={"Authorization": f"Bearer {config.UPSTASH_REDIS_REST_TOKEN}"},
            json=["SET", config.SNAPSHOT_KEY, corpo],
            timeout=10,
        )
        resp.raise_for_status()
        if _ultimo_erro_upstash:
            log("Publicação no Upstash voltou a funcionar.", "SNAPSHOT")
        _ultimo_erro_upstash = None
    except Exception as e:
        # Loga só quando o erro muda, para não poluir o log a cada minuto
        # enquanto o Upstash estiver fora.
        msg = str(e)
        if msg != _ultimo_erro_upstash:
            log(f"Falha ao publicar snapshot no Upstash: {msg}", "SNAPSHOT")
        _ultimo_erro_upstash = msg
