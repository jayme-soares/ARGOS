"""Notificação push por dois canais, em paralelo:

- ntfy: chega em quem assinar o tópico NTFY_TOPIC (app ou site do ntfy).
- Web Push: chega em quem ativou o sino no painel (ver argos/webpush.py).
"""

import requests

from argos import config, webpush
from argos.contratos import SOC, Contrato
from argos.log import log


def enviar_notificacao_push(titulo, mensagem, prioridade=3, tags=None, contrato: Contrato = SOC):
    """Nunca derruba o loop por falha de rede/serviço: só registra o erro.
    Vai para a gestão do `contrato`: o tópico ntfy dele e quem tem acesso a
    ele no painel.

    prioridade: 1 (mín.) a 5 (máx.) — https://docs.ntfy.sh/publish/#message-priority
    tags: emoji shortcodes do ntfy (ex.: ["rotating_light"]) — https://docs.ntfy.sh/publish/#tags-emojis
    """
    if not config.HABILITAR_NOTIFICACAO_PUSH:
        log(f"(push desativado) {titulo}: {mensagem}", "NOTIFICAÇÃO")
        return
    if webpush.habilitado():
        webpush.enviar(titulo, mensagem, prioridade, contrato=contrato)
    _enviar_ntfy(titulo, mensagem, prioridade, tags, contrato)


def _enviar_ntfy(titulo, mensagem, prioridade, tags, contrato):
    if not contrato.ntfy_topic:
        return
    payload = {
        "topic": contrato.ntfy_topic,
        "title": titulo,
        "message": mensagem,
        "priority": prioridade,
        "tags": tags or [],
    }
    if config.PAINEL_URL:
        payload["click"] = webpush.url_painel_gestao(contrato)
    try:
        resp = requests.post(config.NTFY_SERVER, json=payload, timeout=10)
        resp.raise_for_status()
        log(f"Push ntfy enviado: {titulo}", "NOTIFICAÇÃO")
    except Exception as e:
        log(f"Falha ao enviar push via ntfy: {e}", "NOTIFICAÇÃO")
