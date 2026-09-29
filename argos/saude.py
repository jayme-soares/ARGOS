"""Status por monitor, health-check HTTP local e watchdog."""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from argos import config, publicador
from argos.log import log

_lock = threading.Lock()
_ultimo_progresso: dict[str, float] = {}


def atualizar_status(worker: str, estado: str):
    """Registra progresso do monitor (alimenta o watchdog) e o estado que
    aparece no painel."""
    bater(worker)
    publicador.atualizar_status(worker, estado)


def bater(worker: str):
    """Só marca progresso, sem mudar o estado (usado em esperas longas
    legítimas, como o polling da lista de exportação)."""
    with _lock:
        _ultimo_progresso[worker] = time.monotonic()


def dormir(worker: str, segundos: float):
    """time.sleep em fatias de até 60s, batendo o watchdog entre elas —
    uma espera longa de propósito não é travamento."""
    fim = time.monotonic() + segundos
    while True:
        restante = fim - time.monotonic()
        if restante <= 0:
            return
        time.sleep(min(60, restante))
        bater(worker)


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        corpo = json.dumps(publicador.obter_status(), ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def log_message(self, format, *args):
        pass


def iniciar_servidor_saude():
    try:
        servidor = ThreadingHTTPServer(("0.0.0.0", config.PORTA_SAUDE), _HealthHandler)
    except OSError as e:
        log(f"Não consegui abrir o health-check na porta {config.PORTA_SAUDE}: {e}", "HTTP")
        return
    threading.Thread(target=servidor.serve_forever, daemon=True, name="saude").start()
    log(f"Health-check ouvindo na porta {config.PORTA_SAUDE}", "HTTP")


def _watchdog_loop():
    while True:
        time.sleep(30)
        agora = time.monotonic()
        with _lock:
            parados = {w: agora - t for w, t in _ultimo_progresso.items()}
        for worker, parado_ha in parados.items():
            if parado_ha > config.WATCHDOG_SEM_PROGRESSO_SEGUNDOS:
                log(
                    f"Monitor '{worker}' sem progresso há {parado_ha:.0f}s "
                    f"(limite: {config.WATCHDOG_SEM_PROGRESSO_SEGUNDOS}s) — provavelmente preso numa "
                    f"sessão do Chrome travada. Encerrando o processo para o Docker reiniciar o container.",
                    "WATCHDOG",
                )
                os._exit(1)


def iniciar_watchdog():
    threading.Thread(target=_watchdog_loop, daemon=True, name="watchdog").start()
