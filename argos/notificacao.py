"""Notificação push via ntfy — chega no celular e no desktop de quem assinar
o tópico NTFY_TOPIC (app Android/iOS, navegador ou app desktop do ntfy)."""

import requests

from argos import config
from argos.log import log


def enviar_notificacao_push(titulo, mensagem, prioridade=3, tags=None):
    """Nunca derruba o loop por falha de rede/serviço: só registra o erro.

    prioridade: 1 (mín.) a 5 (máx.) — https://docs.ntfy.sh/publish/#message-priority
    tags: emoji shortcodes do ntfy (ex.: ["rotating_light"]) — https://docs.ntfy.sh/publish/#tags-emojis
    """
    if not config.HABILITAR_NOTIFICACAO_PUSH or not config.NTFY_TOPIC:
        log(f"(push desativado) {titulo}: {mensagem}", "NOTIFICAÇÃO")
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
        log(f"Push enviado: {titulo}", "NOTIFICAÇÃO")
    except Exception as e:
        log(f"Falha ao enviar push via ntfy: {e}", "NOTIFICAÇÃO")
