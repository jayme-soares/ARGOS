"""Alertas push de vencimento das religas em campo.

Regras:
- Para cada antecedência em ALERTAS_MINUTOS (padrão 60 e 30 min) e para
  "vencida", cada ordem recebe o aviso UMA única vez. Se a ordem já é vista
  num nível mais urgente (ex.: aparece pela primeira vez faltando 20 min),
  só o nível mais urgente dispara e os menos urgentes ficam marcados como
  enviados — não faz sentido avisar "vence em 1h" depois de "vence em 30 min".
- Um push agrupado por nível por avaliação, não um por ordem.
- Enquanto houver vencidas em aberto, um lembrete com a lista sai a cada
  LEMBRETE_VENCIDAS_MINUTOS.
- Ordens que saem do relatório (foram finalizadas) são removidas do estado.
- Se o vencimento de uma ordem mudar (reprogramação), os avisos dela zeram.

`calcular_alertas` é pura (não envia nada nem toca em disco) para poder ser
testada com horários simulados; `processar_alertas` faz o resto.
"""

from datetime import datetime, timedelta

from argos import config
from argos.estado import carregar_json, salvar_json
from argos.notificacao import enviar_notificacao_push

ARQUIVO_ESTADO = "alertas.json"
NIVEL_VENCIDA = "vencida"
MAX_LINHAS_PUSH = 8


def _nivel_antecedencia(minutos: int) -> str:
    return f"{minutos}min"


def formatar_duracao(minutos: float) -> str:
    minutos = int(round(abs(minutos)))
    h, m = divmod(minutos, 60)
    if h and m:
        return f"{h}h{m:02d}"
    if h:
        return f"{h}h"
    return f"{m} min"


def _linha(reg: dict, venc: datetime, agora: datetime) -> str:
    restante = (venc - agora).total_seconds() / 60
    hora = venc.strftime("%H:%M") if venc.date() == agora.date() else venc.strftime("%d/%m %H:%M")
    quando = f"venceu {hora} (há {formatar_duracao(restante)})" if restante <= 0 else f"vence {hora} (em {formatar_duracao(restante)})"
    partes = [f"TdC {reg.get('tdc')}", reg.get("equipe") or "sem equipe", reg.get("bairro") or "", quando]
    return "• " + " · ".join(p for p in partes if p)


def _mensagem(itens: list[tuple[dict, datetime]], agora: datetime) -> str:
    itens = sorted(itens, key=lambda x: x[1])
    linhas = [_linha(r, v, agora) for r, v in itens[:MAX_LINHAS_PUSH]]
    if len(itens) > MAX_LINHAS_PUSH:
        linhas.append(f"+{len(itens) - MAX_LINHAS_PUSH} outra(s) — veja o painel.")
    return "\n".join(linhas)


def _plural(n: int) -> str:
    return "religa" if n == 1 else "religas"


def calcular_alertas(registros: list[dict], agora: datetime, estado: dict | None, antecedencias=None):
    """Retorna (avisos, novo_estado). Cada aviso é um dict com os argumentos
    de enviar_notificacao_push (titulo, mensagem, prioridade, tags)."""
    antecedencias = sorted(antecedencias if antecedencias is not None else config.ALERTAS_MINUTOS, reverse=True)
    estado = estado or {}
    ordens_ant = estado.get("ordens", {})
    ultimo_lembrete = estado.get("ultimo_lembrete")
    ultimo_lembrete = datetime.fromisoformat(ultimo_lembrete) if ultimo_lembrete else None

    disparar: dict[str, list[tuple[dict, datetime]]] = {}
    vencidas: list[tuple[dict, datetime]] = []
    ordens_novas = {}

    for reg in registros:
        tdc = reg.get("tdc")
        if not tdc or not reg.get("vencimento"):
            continue
        venc = datetime.fromisoformat(reg["vencimento"])
        entrada = ordens_ant.get(tdc)
        if entrada is None or entrada.get("vencimento") != reg["vencimento"]:
            entrada = {"vencimento": reg["vencimento"], "niveis": []}
        else:
            entrada = {"vencimento": entrada["vencimento"], "niveis": list(entrada.get("niveis", []))}

        restante = (venc - agora).total_seconds() / 60
        if restante <= 0:
            nivel = NIVEL_VENCIDA
            menos_urgentes = [_nivel_antecedencia(a) for a in antecedencias]
            vencidas.append((reg, venc))
        else:
            aplicaveis = [a for a in antecedencias if restante <= a]
            if aplicaveis:
                mais_urgente = min(aplicaveis)
                nivel = _nivel_antecedencia(mais_urgente)
                menos_urgentes = [_nivel_antecedencia(a) for a in antecedencias if a > mais_urgente]
            else:
                nivel, menos_urgentes = None, []

        if nivel and nivel not in entrada["niveis"]:
            disparar.setdefault(nivel, []).append((reg, venc))
            entrada["niveis"].append(nivel)
        for n in menos_urgentes:
            if n not in entrada["niveis"]:
                entrada["niveis"].append(n)

        ordens_novas[tdc] = entrada

    avisos = []
    for a in antecedencias:
        itens = disparar.get(_nivel_antecedencia(a))
        if not itens:
            continue
        mais_urgente = a == min(antecedencias)
        avisos.append({
            "titulo": f"ARGOS · {len(itens)} {_plural(len(itens))} {'vence' if len(itens) == 1 else 'vencem'} em até {formatar_duracao(a)}",
            "mensagem": _mensagem(itens, agora),
            "prioridade": 5 if mais_urgente else 4,
            "tags": ["hourglass_flowing_sand"] if mais_urgente else ["warning"],
        })

    itens_vencidos = disparar.get(NIVEL_VENCIDA)
    if itens_vencidos:
        avisos.append({
            "titulo": f"ARGOS · {len(itens_vencidos)} {_plural(len(itens_vencidos))} {'VENCEU' if len(itens_vencidos) == 1 else 'VENCERAM'} sem finalizar",
            "mensagem": _mensagem(itens_vencidos, agora),
            "prioridade": 5,
            "tags": ["rotating_light"],
        })
        # O próprio aviso de vencida conta como lembrete — o próximo resumo
        # sai só daqui a LEMBRETE_VENCIDAS_MINUTOS.
        ultimo_lembrete = agora
    elif vencidas:
        if ultimo_lembrete is None or agora - ultimo_lembrete >= timedelta(minutes=config.LEMBRETE_VENCIDAS_MINUTOS):
            avisos.append({
                "titulo": f"ARGOS · {len(vencidas)} {_plural(len(vencidas))} {'vencida' if len(vencidas) == 1 else 'vencidas'} em aberto",
                "mensagem": _mensagem(vencidas, agora),
                "prioridade": 4,
                "tags": ["alarm_clock"],
            })
            ultimo_lembrete = agora
    else:
        ultimo_lembrete = None

    novo_estado = {
        "ordens": ordens_novas,
        "ultimo_lembrete": ultimo_lembrete.isoformat(timespec="seconds") if ultimo_lembrete else None,
        "avaliado_em": agora.isoformat(timespec="seconds"),
    }
    return avisos, novo_estado


def processar_alertas(registros: list[dict], agora: datetime | None = None) -> list[dict]:
    agora = agora or datetime.now(config.TIMEZONE)
    avisos, novo_estado = calcular_alertas(registros, agora, carregar_json(ARQUIVO_ESTADO))
    # Salva antes de enviar: se o processo cair no meio do envio, é melhor
    # perder um push do que repetir todos no restart.
    salvar_json(ARQUIVO_ESTADO, novo_estado)
    for aviso in avisos:
        enviar_notificacao_push(**aviso)
    return avisos
