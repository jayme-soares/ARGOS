import unittest
from datetime import datetime, timedelta

from argos.alertas import PROGRAMAVEIS, calcular_alertas
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

    def test_niveis_padrao_2h_1h_30_15(self):
        regs = [reg("A", 150)]
        enviados = []
        estado = None
        for minuto in range(0, 160, 5):
            avisos, estado = calcular_alertas(regs, T0 + timedelta(minutes=minuto), estado, antecedencias=[120, 60, 30, 15])
            enviados += [a["titulo"] for a in avisos]
        self.assertEqual(len(enviados), 5)
        for trecho, titulo in zip(["em até 2h", "em até 1h", "em até 30 min", "em até 15 min", "VENCEU"], enviados):
            self.assertIn(trecho, titulo)

    def test_prioridade_maxima_ate_30_min(self):
        avisos, _ = calcular_alertas([reg("A", 100), reg("B", 50), reg("C", 25), reg("D", 10)], T0, None,
                                     antecedencias=[120, 60, 30, 15])
        self.assertEqual([a["prioridade"] for a in avisos], [4, 4, 5, 5])

    def test_programaveis(self):
        avisos, estado = calcular_alertas([reg("A", 100), reg("D", -10)], T0, None,
                                          antecedencias=[120, 60, 30, 15], categoria=PROGRAMAVEIS)
        self.assertIn("1 programável vence em até 2h", avisos[0]["titulo"])
        self.assertIn("VENCEU sem designar", avisos[1]["titulo"])
        # Programáveis não têm lembrete periódico das vencidas.
        avisos, _ = calcular_alertas([reg("D", -10)], T0 + timedelta(hours=2), estado,
                                     antecedencias=[120, 60, 30, 15], categoria=PROGRAMAVEIS)
        self.assertEqual(avisos, [])

    def test_leitura_parcial_mantem_estado(self):
        _, estado = self.avaliar([reg("A", 20), reg("B", 20)], 0, None)
        # B não apareceu (ficou fora da 1ª página), mas a leitura é parcial.
        _, estado = calcular_alertas([reg("A", 20)], T0 + timedelta(minutes=1), estado, antecedencias=[60, 30], parcial=True)
        self.assertEqual(set(estado["ordens"]), {"A", "B"})
        # B volta: não repete o aviso.
        avisos, _ = self.avaliar([reg("A", 20), reg("B", 20)], 2, estado)
        self.assertEqual(avisos, [])

    def test_sem_vencimento_ignorado(self):
        avisos, estado = self.avaliar([{"tdc": "X", "vencimento": None}], 0, None)
        self.assertEqual(avisos, [])
        self.assertEqual(estado["ordens"], {})


if __name__ == "__main__":
    unittest.main()
