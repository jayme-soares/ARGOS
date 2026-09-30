"""Notificação push por dois canais, em paralelo:

- ntfy: chega em quem assinar o tópico NTFY_TOPIC (app ou site do ntfy).
- Web Push: chega em quem ativou o sino no painel (ver argos/webpush.py).
"""

import requests

from argos import config, webpush
from argos.log import log


def enviar_notificacao_push(titulo, mensagem, prioridade=3, tags=None):
    """Nunca derruba o loop por falha de rede/serviço: só registra o erro.

    prioridade: 1 (mín.) a 5 (máx.) — https://docs.ntfy.sh/publish/#message-priority
    tags: emoji shortcodes do ntfy (ex.: ["rotating_light"]) — https://docs.ntfy.sh/publish/#tags-emojis
    """
    if not config.HABILITAR_NOTIFICACAO_PUSH:
        log(f"(push desativado) {titulo}: {mensagem}", "NOTIFICAÇÃO")
        return
    if webpush.habilitado():
        webpush.enviar(titulo, mensagem, prioridade)
    _enviar_ntfy(titulo, mensagem, prioridade, tags)


def _enviar_ntfy(titulo, mensagem, prioridade, tags):
    if not config.NTFY_TOPIC:
        return
    payload = {
        "topic": config.NTFY_TOPIC,
        "title": titulo,
        "message": mensagem,
        "priority": prioridade,
        "tags": tags or [],
    }
    if config.PAINEL_URL:
        payload["click"] = config.PAINEL_URL
    try:
        resp = requests.post(config.NTFY_SERVER, json=payload, timeout=10)
        resp.raise_for_status()
        log(f"Push ntfy enviado: {titulo}", "NOTIFICAÇÃO")
    except Exception as e:
        log(f"Falha ao enviar push via ntfy: {e}", "NOTIFICAÇÃO")
