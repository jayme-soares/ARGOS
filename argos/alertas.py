"""Alertas push de vencimento, em duas categorias independentes:

- CAMPO: religas em campo (designadas a uma equipe NI2, ainda em aberto).
- PROGRAMAVEIS: religações programáveis ainda não designadas.

Cada categoria tem seu próprio arquivo de estado: uma ordem que passa de
programável para em campo começa os avisos de novo, agora como "em campo".

Regras:
- Para cada antecedência em ALERTAS_MINUTOS (padrão 2h, 1h, 30 e 15 min) e para
  "vencida", cada ordem recebe o aviso UMA única vez. Se a ordem já é vista
  num nível mais urgente (ex.: aparece pela primeira vez faltando 20 min),
  só o nível mais urgente dispara e os menos urgentes ficam marcados como
  enviados — não faz sentido avisar "vence em 1h" depois de "vence em 30 min".
- Um push agrupado por nível por avaliação, não um por ordem.
- Só em CAMPO: enquanto houver vencidas em aberto, um lembrete com a lista
  sai a cada LEMBRETE_VENCIDAS_MINUTOS.
- Ordens que saem do relatório (foram finalizadas) são removidas do estado,
  exceto quando a leitura é parcial (só a 1ª página das programáveis): aí a
  ausência não prova nada e o estado delas é mantido, para não repetir avisos.
- Se o vencimento de uma ordem mudar (reprogramação), os avisos dela zeram.

`calcular_alertas` é pura (não envia nada nem toca em disco) para poder ser
testada com horários simulados; `processar_alertas` faz o resto.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from argos import config
from argos.estado import carregar_json, salvar_json
from argos.notificacao import enviar_notificacao_push

NIVEL_VENCIDA = "vencida"
MAX_LINHAS_PUSH = 8
# Antecedências até aqui (inclusive) usam prioridade máxima no push.
MINUTOS_PRIORIDADE_MAXIMA = 30


@dataclass(frozen=True)
class Categoria:
    arquivo_estado: str
    singular: str
    plural: str
    vencida_sem: str          # "...VENCEU sem <isto>"
    lembrete_vencidas: bool   # lembrete periódico das vencidas em aberto


CAMPO = Categoria("alertas.json", "religa", "religas", "finalizar", lembrete_vencidas=True)
PROGRAMAVEIS = Categoria("alertas_programaveis.json", "programável", "programáveis", "designar", lembrete_vencidas=False)


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


def linha_push(tdc: str, venc: datetime | None) -> str:
    """Push enxuto: só o TdC e a data/hora do vencimento — o resto está no painel."""
    return f"• TdC {tdc} — {venc:%d/%m %H:%M}" if venc else f"• TdC {tdc}"


def _mensagem(itens: list[tuple[dict, datetime]], agora: datetime) -> str:
    itens = sorted(itens, key=lambda x: x[1])
    linhas = [linha_push(r.get("tdc"), v) for r, v in itens[:MAX_LINHAS_PUSH]]
    if len(itens) > MAX_LINHAS_PUSH:
        linhas.append(f"+{len(itens) - MAX_LINHAS_PUSH} outra(s) — veja o painel.")
    return "\n".join(linhas)


def calcular_alertas(registros: list[dict], agora: datetime, estado: dict | None, antecedencias=None,
                     categoria: Categoria = CAMPO, parcial: bool = False):
    """Retorna (avisos, novo_estado). Cada aviso é um dict com os argumentos
    de enviar_notificacao_push (titulo, mensagem, prioridade, tags)."""
    def nome(n: int) -> str:
        return categoria.singular if n == 1 else categoria.plural

    antecedencias = sorted(antecedencias if antecedencias is not None else config.ALERTAS_MINUTOS, reverse=True)
    estado = estado or {}
    ordens_ant = estado.get("ordens", {})
    ultimo_lembrete = estado.get("ultimo_lembrete")
    ultimo_lembrete = datetime.fromisoformat(ultimo_lembrete) if ultimo_lembrete else None

    disparar: dict[str, list[tuple[dict, datetime]]] = {}
    vencidas: list[tuple[dict, datetime]] = []
    # Leitura parcial: quem não apareceu agora continua no estado.
    ordens_novas = dict(ordens_ant) if parcial else {}

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
        urgente = a <= MINUTOS_PRIORIDADE_MAXIMA
        avisos.append({
            "titulo": f"ARGOS · {len(itens)} {nome(len(itens))} {'vence' if len(itens) == 1 else 'vencem'} em até {formatar_duracao(a)}",
            "mensagem": _mensagem(itens, agora),
            "prioridade": 5 if urgente else 4,
            "tags": ["hourglass_flowing_sand"] if urgente else ["warning"],
        })

    itens_vencidos = disparar.get(NIVEL_VENCIDA)
    if itens_vencidos:
        avisos.append({
            "titulo": f"ARGOS · {len(itens_vencidos)} {nome(len(itens_vencidos))} {'VENCEU' if len(itens_vencidos) == 1 else 'VENCERAM'} sem {categoria.vencida_sem}",
            "mensagem": _mensagem(itens_vencidos, agora),
            "prioridade": 5,
            "tags": ["rotating_light"],
        })
        # O próprio aviso de vencida conta como lembrete — o próximo resumo
        # sai só daqui a LEMBRETE_VENCIDAS_MINUTOS.
        ultimo_lembrete = agora
    elif vencidas and categoria.lembrete_vencidas:
        if ultimo_lembrete is None or agora - ultimo_lembrete >= timedelta(minutes=config.LEMBRETE_VENCIDAS_MINUTOS):
            avisos.append({
                "titulo": f"ARGOS · {len(vencidas)} {nome(len(vencidas))} {'vencida' if len(vencidas) == 1 else 'vencidas'} em aberto",
                "mensagem": _mensagem(vencidas, agora),
                "prioridade": 4,
                "tags": ["alarm_clock"],
            })
            ultimo_lembrete = agora
    elif not vencidas:
        ultimo_lembrete = None

    novo_estado = {
        "ordens": ordens_novas,
        "ultimo_lembrete": ultimo_lembrete.isoformat(timespec="seconds") if ultimo_lembrete else None,
        "avaliado_em": agora.isoformat(timespec="seconds"),
    }
    return avisos, novo_estado


def processar_alertas(registros: list[dict], agora: datetime | None = None, categoria: Categoria = CAMPO,
                      parcial: bool = False) -> list[dict]:
    """registros: dicts com "tdc" e "vencimento" (ISO)."""
    agora = agora or datetime.now(config.TIMEZONE)
    avisos, novo_estado = calcular_alertas(
        registros, agora, carregar_json(categoria.arquivo_estado), categoria=categoria, parcial=parcial,
    )
    # Salva antes de enviar: se o processo cair no meio do envio, é melhor
    # perder um push do que repetir todos no restart.
    salvar_json(categoria.arquivo_estado, novo_estado)
    for aviso in avisos:
        enviar_notificacao_push(**aviso)
    return avisos
