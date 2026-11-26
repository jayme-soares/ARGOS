import threading
import time
import unittest
from unittest import mock

from argos import config
from argos.workers import _VezDoCampo


class VezDoCampoTest(unittest.TestCase):
    def rodar(self, vez, prioritario, ordem, nome):
        def alvo():
            with vez.vez(nome, prioritario):
                ordem.append(nome)
        t = threading.Thread(target=alvo, daemon=True)
        t.start()
        return t

    def test_negociacao_espera_a_soc_pendente(self):
        vez, ordem = _VezDoCampo(), []
        vez.marcar_prioritario_pendente(True)
        neg = self.rodar(vez, False, ordem, "neg")
        time.sleep(0.2)
        self.assertEqual(ordem, [])
        with vez.vez("soc", True):
            ordem.append("soc")
        vez.marcar_prioritario_pendente(False)
        neg.join(2)
        self.assertEqual(ordem, ["soc", "neg"])

    def test_soc_espera_ciclo_em_andamento(self):
        vez, ordem = _VezDoCampo(), []
        with vez.vez("neg", False):
            soc = self.rodar(vez, True, ordem, "soc")
            time.sleep(0.2)
            ordem.append("neg")
        soc.join(2)
        self.assertEqual(ordem, ["neg", "soc"])

    def test_negociacao_nao_espera_para_sempre(self):
        vez, ordem = _VezDoCampo(), []
        vez.marcar_prioritario_pendente(True)
        with mock.patch.object(config, "ESPERA_MAX_PRIORIDADE_CAMPO_MINUTOS", 0):
            with vez.vez("neg", False):
                ordem.append("neg")
        self.assertEqual(ordem, ["neg"])


if __name__ == "__main__":
    unittest.main()
