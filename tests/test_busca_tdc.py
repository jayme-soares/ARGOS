"""Desistência da exportação que o eOrder perdeu."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from argos.eorder import busca_tdc


class _Linha:
    def is_displayed(self):
        return True


class _DriverLista:
    """Lista de exportação em que só aparecem as exportações `presentes`."""

    def __init__(self, presentes):
        self.presentes = presentes

    def find_elements(self, _by, xpath):
        return [_Linha()] if any(f"'{n}'" in xpath for n in self.presentes) else []


class DesistenciaExportacaoTest(unittest.TestCase):
    def setUp(self):
        self.pasta = Path(tempfile.mkdtemp())
        for alvo, valor in (("EXPORTACAO_POLLING_SEGUNDOS", 0), ("EXPORTACAO_TIMEOUT_SEGUNDOS", 2)):
            patcher = mock.patch.object(busca_tdc.config, alvo, valor)
            patcher.start()
            self.addCleanup(patcher.stop)
        for alvo in ("_atualizar_lista_exportacao", "debug_screenshot", "log"):
            patcher = mock.patch.object(busca_tdc, alvo)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_desiste_quando_as_posteriores_ja_apareceram(self):
        driver = _DriverLista(["exp_08"])
        with mock.patch.object(busca_tdc.config, "EXPORTACAO_PERDIDA_SEGUNDOS", 0):
            with self.assertRaisesRegex(TimeoutError, "provavelmente a perdeu"):
                busca_tdc.baixar_exportacao(driver, "exp_07", pasta=self.pasta, posteriores=["exp_08", "exp_09"])

    def test_sem_posteriores_espera_o_prazo_inteiro(self):
        driver = _DriverLista([])
        with mock.patch.object(busca_tdc.config, "EXPORTACAO_PERDIDA_SEGUNDOS", 0):
            with self.assertRaisesRegex(TimeoutError, "não ficou disponível"):
                busca_tdc.baixar_exportacao(driver, "exp_07", pasta=self.pasta, posteriores=["exp_08"])


if __name__ == "__main__":
    unittest.main()
