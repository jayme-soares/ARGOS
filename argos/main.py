"""Ponto de entrada:  python -m argos.main"""

import time

from argos import config, publicador, saude
from argos.log import log
from argos.workers import MonitorCampo, MonitorProgramaveis


def main():
    for aviso in config.validar_config():
        log(aviso, "CONFIG")
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    publicador.carregar_snapshot_local()
    saude.iniciar_servidor_saude()

    monitores = [MonitorProgramaveis(), MonitorCampo()]
    for m in monitores:
        saude.bater(m.NOME)
    saude.iniciar_watchdog()
    for m in monitores:
        m.start()
    log(
        f"ARGOS iniciado (janela {config.HORARIO_INICIO}h-{config.HORARIO_FIM}h, "
        f"modo {'sequencial' if config.MODO_SEQUENCIAL else 'paralelo'}).",
    )

    try:
        while True:
            time.sleep(60)
            mortos = [m.NOME for m in monitores if not m.is_alive()]
            if mortos:
                # Uma exceção não tratada matou a thread: melhor o container
                # reiniciar do que o painel ficar meio parado sem ninguém ver.
                log(f"Monitor(es) parado(s) inesperadamente: {mortos}. Encerrando para o Docker reiniciar.")
                raise SystemExit(1)
    except KeyboardInterrupt:
        log("Encerrado pelo usuário.")


if __name__ == "__main__":
    main()
