"""Persistência simples em JSON dentro de DATA_DIR (volume do Docker)."""

import json
import os
import threading
from pathlib import Path

from argos.config import DATA_DIR

_lock = threading.Lock()


def caminho(nome: str) -> Path:
    return DATA_DIR / nome


def carregar_json(nome: str, padrao=None):
    """Retorna `padrao` se o arquivo não existe ou está corrompido."""
    arq = caminho(nome)
    if not arq.exists():
        return padrao
    try:
        return json.loads(arq.read_text(encoding="utf-8"))
    except Exception:
        return padrao


def salvar_json(nome: str, dados):
    """Grava num temporário e troca de uma vez (os.replace): se o processo
    morrer no meio (watchdog, restart do container), o arquivo antigo fica
    intacto em vez de corrompido pela metade."""
    arq = caminho(nome)
    with _lock:
        arq.parent.mkdir(parents=True, exist_ok=True)
        tmp = arq.with_suffix(arq.suffix + ".tmp")
        tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, arq)
