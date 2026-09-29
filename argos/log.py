from datetime import datetime

from argos.config import TIMEZONE


def log(mensagem: str, prefixo: str | None = None):
    """print com timestamp de Brasília e prefixo opcional ([CAMPO],
    [PROGRAMÁVEIS] etc.). flush=True porque o Docker só mostra o log quando o
    buffer esvazia."""
    agora = datetime.now(TIMEZONE).strftime("%d/%m/%Y %H:%M:%S")
    tag = f" [{prefixo}]" if prefixo else ""
    print(f"[{agora}]{tag} {mensagem}", flush=True)
