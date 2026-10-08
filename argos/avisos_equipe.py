"""Avisos às equipes de campo, com confirmação obrigatória.

Cada equipe (código NI2...) com conta aprovada no painel /equipe recebe
push só das próprias ordens:
- "designada": a religa apareceu na exportação para a equipe (nova ou
  redesignada de outra equipe);
- "vencer": a religa chegou a um dos níveis de ALERTAS_MINUTOS (os mesmos
  disparos dos alertas da gestão, ver alertas.calcular_disparos);
- "vencida": a religa venceu sem finalizar.

Cada aviso vira uma linha em public.argos_avisos_equipe (Supabase), uma por
ordem e evento, e fica "pendente" até a equipe tocar em "Confirmo que
visualizei" no painel. Enquanto pendente, o push é reenviado a cada
REENVIO_AVISO_MINUTOS. Se a ordem sai da equipe (finalizada ou
redesignada), o aviso é "dispensado" e para de exigir confirmação.

O Supabase só grava avisos de equipes com conta aprovada; equipes sem conta
são ignoradas sem erro.

Teste manual (cria um aviso fictício e manda o push):
    python -m argos.avisos_equipe --teste NI201LRP-B
"""

import re
import sys
from datetime import datetime, timedelta

from argos import config, webpush
from argos.alertas import MAX_LINHAS_PUSH, MINUTOS_PRIORIDADE_MAXIMA, NIVEL_VENCIDA, formatar_duracao
from argos.estado import carregar_json, salvar_json
from argos.log import log

PREFIXO = "EQUIPES"
ARQUIVO_DESIGNACOES = "designacoes.json"
# TdC do aviso de teste (--teste): não é dispensado por não estar em aberto,
# fica pendente (e sendo reenviado) até a equipe confirmar.
TDC_TESTE = "TESTE"


def normalizar_equipe(equipe) -> str:
    return re.sub(r"\s+", "", str(equipe or "")).upper()


def habilitado() -> bool:
    return config.AVISOS_EQUIPE and config.HABILITAR_NOTIFICACAO_PUSH and webpush.habilitado()


# ------------------------------------------------------------------
# Funções puras (testadas em tests/test_avisos_equipe.py)
# ------------------------------------------------------------------

def detectar_designacoes(anteriores: dict | None, registros: list[dict]) -> tuple[list[dict], dict]:
    """Compara {tdc: equipe} da exportação anterior com a atual.

    Retorna (registros recém-designados, nova base). Sem base anterior
    (primeira execução) não avisa nada, só grava a base — senão todas as
    ordens em aberto chegariam como "designadas". Uma exportação vazia não
    substitui a base, para não avisar tudo de novo na exportação seguinte.
    """
    atuais = {}
    for r in registros:
        equipe = normalizar_equipe(r.get("equipe"))
        if r.get("tdc") and equipe:
            atuais[r["tdc"]] = equipe
    if anteriores is None:
        return [], atuais
    if not atuais:
        return [], anteriores
    novas = [r for r in registros if r.get("tdc") in atuais and anteriores.get(r["tdc"]) != atuais[r["tdc"]]]
    return novas, atuais


def _venc(reg) -> datetime | None:
    return datetime.fromisoformat(reg["vencimento"]) if reg.get("vencimento") else None


def _minutos(nivel: str | None) -> int | None:
    m = re.fullmatch(r"(\d+)min", nivel or "")
    return int(m.group(1)) if m else None


def _titulo_ordem(tipo: str, nivel: str | None, reg: dict) -> str:
    tdc = reg.get("tdc")
    if tipo == "designada":
        return f"Nova religa designada · TdC {tdc}"
    if tipo == "vencida":
        return f"Religa VENCIDA · TdC {tdc}"
    return f"Religa vence em até {formatar_duracao(_minutos(nivel) or 0)} · TdC {tdc}"


def _detalhe(reg: dict) -> str:
    partes = [reg.get("endereco"), reg.get("bairro")]
    venc = _venc(reg)
    if venc:
        partes.append(f"vence {venc:%d/%m %H:%M}")
    return " · ".join(p for p in partes if p)


def montar_avisos(itens: list[tuple[dict, str, str | None]]) -> tuple[list[dict], list[dict]]:
    """itens: [(registro, tipo, nivel)].

    Retorna (linhas para argos_avisos_equipe, pushes). Um push por equipe e
    por tipo/nível, agrupando as ordens, no estilo dos alertas da gestão.
    """
    linhas, grupos = [], {}
    for reg, tipo, nivel in itens:
        equipe = normalizar_equipe(reg.get("equipe"))
        if not equipe or not reg.get("tdc"):
            continue
        linhas.append({
            "equipe": equipe,
            "tipo": tipo,
            "nivel": nivel,
            "tdc": reg["tdc"],
            "ordem": reg.get("ordem"),
            "vencimento": reg.get("vencimento"),
            "titulo": _titulo_ordem(tipo, nivel, reg),
            "mensagem": _detalhe(reg),
        })
        grupos.setdefault((equipe, tipo, nivel), []).append(reg)

    pushes = []
    for (equipe, tipo, nivel), regs in grupos.items():
        n = len(regs)
        nome = "religa" if n == 1 else "religas"
        if tipo == "designada":
            titulo = f"ARGOS · {n} {'nova religa designada' if n == 1 else 'novas religas designadas'} para {equipe}"
            prioridade = 4
        elif tipo == "vencida":
            titulo = f"ARGOS · {n} {nome} {'VENCEU' if n == 1 else 'VENCERAM'} sem finalizar"
            prioridade = 5
        else:
            minutos = _minutos(nivel) or 0
            titulo = f"ARGOS · {n} {nome} {'vence' if n == 1 else 'vencem'} em até {formatar_duracao(minutos)}"
            prioridade = 5 if minutos <= MINUTOS_PRIORIDADE_MAXIMA else 4
        regs = sorted(regs, key=lambda r: r.get("vencimento") or "")
        linhas_push = []
        for r in regs[:MAX_LINHAS_PUSH]:
            venc = _venc(r)
            local = r.get("bairro") or ""
            linhas_push.append(f"• TdC {r['tdc']}" + (f" — {venc:%d/%m %H:%M}" if venc else "") + (f" · {local}" if local else ""))
        if n > MAX_LINHAS_PUSH:
            linhas_push.append(f"… e mais {n - MAX_LINHAS_PUSH}")
        linhas_push.append("Abra o painel e confirme que visualizou.")
        pushes.append({
            "equipe": equipe,
            "titulo": titulo,
            "mensagem": "\n".join(linhas_push),
            "prioridade": prioridade,
            "tag": f"argos-{tipo}-{nivel or ''}",
        })
    return linhas, pushes


def classificar_pendentes(pendentes: list[dict], registros: list[dict], agora: datetime,
                          intervalo_min: int) -> tuple[list[int], dict[str, list[dict]]]:
    """Retorna (ids a dispensar, {equipe: avisos a reenviar}).

    Dispensa os avisos cuja ordem não está mais em aberto para aquela equipe
    (finalizada ou redesignada). Reenvia os que estão sem envio há pelo menos
    `intervalo_min` minutos.
    """
    em_aberto = {(r.get("tdc"), normalizar_equipe(r.get("equipe"))) for r in registros}
    dispensar, reenviar = [], {}
    limite = agora - timedelta(minutes=intervalo_min)
    for a in pendentes:
        if a.get("tdc") != TDC_TESTE and (a.get("tdc"), normalizar_equipe(a.get("equipe"))) not in em_aberto:
            dispensar.append(a["id"])
            continue
        ultimo = a.get("ultimo_envio_em")
        if ultimo is None or datetime.fromisoformat(ultimo) <= limite:
            reenviar.setdefault(normalizar_equipe(a["equipe"]), []).append(a)
    return dispensar, reenviar


def montar_lembrete(equipe: str, avisos: list[dict]) -> dict:
    n = len(avisos)
    linhas = [f"• {a['titulo']}" for a in avisos[:MAX_LINHAS_PUSH]]
    if n > MAX_LINHAS_PUSH:
        linhas.append(f"… e mais {n - MAX_LINHAS_PUSH}")
    linhas.append("Abra o painel e confirme que visualizou.")
    return {
        "equipe": equipe,
        "titulo": f"ARGOS · {n} {'aviso' if n == 1 else 'avisos'} sem confirmar",
        "mensagem": "\n".join(linhas),
        "prioridade": 5,
        "tag": "argos-pendentes",
    }


# ------------------------------------------------------------------
# Envio (Supabase + Web Push). Nunca derruba o monitor: só loga.
# ------------------------------------------------------------------

def _enviar(itens: list[tuple[dict, str, str | None]]):
    if not itens or not habilitado():
        return
    linhas, pushes = montar_avisos(itens)
    if not linhas:
        return
    try:
        criados = webpush._rpc("argos_avisos_criar", {"p_chave": config.PUSH_CHAVE, "p_avisos": linhas}) or []
    except Exception as e:
        log(f"Falha ao registrar avisos das equipes: {e}", PREFIXO)
        return
    com_conta = {normalizar_equipe(c.get("equipe")) for c in criados}
    for p in pushes:
        if p["equipe"] in com_conta:
            webpush.enviar(p["titulo"], p["mensagem"], p["prioridade"], equipe=p["equipe"], tag=p["tag"])
    if criados:
        log(f"{len(criados)} aviso(s) registrado(s) para {len(com_conta)} equipe(s): {', '.join(sorted(com_conta))}.", PREFIXO)


def avisar_designacoes(registros: list[dict]):
    """Chamada a cada exportação, com as ordens em aberto."""
    anteriores = carregar_json(ARQUIVO_DESIGNACOES)
    novas, base = detectar_designacoes(anteriores, registros)
    salvar_json(ARQUIVO_DESIGNACOES, base)
    if anteriores is None:
        log(f"Base de designações criada com {len(base)} ordem(ns); avisos de designação a partir da próxima exportação.", PREFIXO)
    _enviar([(r, "designada", None) for r in novas])


def avisar_vencimentos(disparos: list[tuple[dict, str]], agora: datetime | None = None):
    """Callback de alertas.processar_alertas: [(registro, nivel)]."""
    _enviar([(reg, "vencida" if nivel == NIVEL_VENCIDA else "vencer", nivel) for reg, nivel in disparos])


def reenviar_pendentes(registros: list[dict], agora: datetime):
    """Chamada a cada reavaliação dos alertas (~5 min), sobre dados frescos."""
    if not habilitado():
        return
    try:
        pendentes = webpush._rpc("argos_avisos_pendentes", {"p_chave": config.PUSH_CHAVE}) or []
    except Exception as e:
        log(f"Falha ao buscar avisos pendentes: {e}", PREFIXO)
        return
    if not pendentes:
        return
    dispensar, reenviar = classificar_pendentes(pendentes, registros, agora, config.REENVIO_AVISO_MINUTOS)
    try:
        if dispensar:
            webpush._rpc("argos_avisos_dispensar", {"p_chave": config.PUSH_CHAVE, "p_ids": dispensar})
            log(f"{len(dispensar)} aviso(s) dispensado(s): a ordem saiu da equipe.", PREFIXO)
        for equipe, avisos in reenviar.items():
            p = montar_lembrete(equipe, avisos)
            webpush.enviar(p["titulo"], p["mensagem"], p["prioridade"], equipe=equipe, tag=p["tag"])
            webpush._rpc("argos_avisos_marcar_envio", {"p_chave": config.PUSH_CHAVE, "p_ids": [a["id"] for a in avisos]})
    except Exception as e:
        log(f"Falha ao processar avisos pendentes: {e}", PREFIXO)


def _teste(equipe: str):
    if not habilitado():
        print("Push desativado ou incompleto (ARGOS_AVISOS_EQUIPE/ARGOS_PUSH/SUPABASE_*/ARGOS_PUSH_CHAVE/VAPID).")
        return
    agora = datetime.now(config.TIMEZONE)
    reg = {"tdc": TDC_TESTE, "ordem": TDC_TESTE, "equipe": equipe, "bairro": "Teste do ARGOS",
           "vencimento": (agora + timedelta(hours=1)).isoformat(timespec="minutes")}
    _enviar([(reg, "designada", None)])
    print("Aviso de teste enviado. Fica pendente (e é reenviado pelo bot) até a equipe confirmar no painel.")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--teste":
        _teste(normalizar_equipe(sys.argv[2]))
    else:
        print("Uso: python -m argos.avisos_equipe --teste <código da equipe>")
