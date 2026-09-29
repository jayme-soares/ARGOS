import unittest
from datetime import datetime, timedelta

from argos.alertas import calcular_alertas
from argos.config import TIMEZONE

T0 = datetime(2026, 9, 29, 10, 0, tzinfo=TIMEZONE)


def reg(tdc, minutos, equipe="EQ1", bairro="Centro"):
    venc = (T0 + timedelta(minutes=minutos)).isoformat(timespec="minutes")
    return {"tdc": tdc, "equipe": equipe, "bairro": bairro, "vencimento": venc}


class TestAlertas(unittest.TestCase):
    def avaliar(self, registros, minutos_depois, estado):
        return calcular_alertas(registros, T0 + timedelta(minutes=minutos_depois), estado, antecedencias=[60, 30])

    def test_cada_nivel_dispara_uma_vez(self):
        regs = [reg("A", 90), reg("B", 55), reg("C", 25), reg("D", -10)]
        avisos, estado = self.avaliar(regs, 0, None)
        titulos = [a["titulo"] for a in avisos]
        self.assertEqual(len(avisos), 3)
        self.assertIn("1 religa vence em até 1h", titulos[0])      # B
        self.assertIn("1 religa vence em até 30 min", titulos[1])  # C (sem aviso de 1h)
        self.assertIn("VENCEU", titulos[2])                         # D
        self.assertIn("TdC B", avisos[0]["mensagem"])
        self.assertEqual(sorted(estado["ordens"]["C"]["niveis"]), ["30min", "60min"])

        # Mesma avaliação de novo: nada novo.
        avisos, estado = self.avaliar(regs, 1, estado)
        self.assertEqual(avisos, [])

        # 35 min depois: A entra em 1h, B entra em 30 min, C vence.
        avisos, estado = self.avaliar(regs, 35, estado)
        textos = "\n".join(a["titulo"] + a["mensagem"] for a in avisos)
        self.assertIn("TdC A", textos)
        self.assertIn("TdC B", textos)
        self.assertIn("VENCEU", textos)
        self.assertIn("TdC C", textos)

    def test_lembrete_de_vencidas_a_cada_hora(self):
        regs = [reg("D", -10)]
        avisos, estado = self.avaliar(regs, 0, None)
        self.assertEqual(len(avisos), 1)  # aviso de vencida

        avisos, estado = self.avaliar(regs, 30, estado)
        self.assertEqual(avisos, [])

        avisos, estado = self.avaliar(regs, 60, estado)
        self.assertEqual(len(avisos), 1)
        self.assertIn("vencida em aberto", avisos[0]["titulo"])

        avisos, estado = self.avaliar(regs, 90, estado)
        self.assertEqual(avisos, [])

    def test_finalizada_sai_do_estado(self):
        avisos, estado = self.avaliar([reg("D", -10), reg("E", 20)], 0, None)
        self.assertEqual(set(estado["ordens"]), {"D", "E"})
        avisos, estado = self.avaliar([reg("E", 20)], 5, estado)
        self.assertEqual(set(estado["ordens"]), {"E"})
        self.assertEqual(avisos, [])

    def test_sem_vencidas_zera_lembrete(self):
        _, estado = self.avaliar([reg("D", -10)], 0, None)
        _, estado = self.avaliar([], 5, estado)
        self.assertIsNone(estado["ultimo_lembrete"])

    def test_reprogramacao_zera_avisos(self):
        _, estado = self.avaliar([reg("A", 20)], 0, None)
        avisos, _ = self.avaliar([reg("A", 50)], 1, estado)  # vencimento mudou
        self.assertEqual(len(avisos), 1)
        self.assertIn("1h", avisos[0]["titulo"])

    def test_sem_vencimento_ignorado(self):
        avisos, estado = self.avaliar([{"tdc": "X", "vencimento": None}], 0, None)
        self.assertEqual(avisos, [])
        self.assertEqual(estado["ordens"], {})


if __name__ == "__main__":
    unittest.main()
