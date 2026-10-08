import unittest
from datetime import datetime, timedelta

from argos.alertas import calcular_disparos
from argos.avisos_equipe import (
    TDC_TESTE,
    classificar_pendentes,
    detectar_designacoes,
    montar_avisos,
    montar_lembrete,
)
from argos.config import TIMEZONE

T0 = datetime(2026, 10, 8, 10, 0, tzinfo=TIMEZONE)


def reg(tdc, equipe, minutos=120, bairro="Centro"):
    venc = (T0 + timedelta(minutes=minutos)).isoformat(timespec="minutes")
    return {"tdc": tdc, "ordem": f"O{tdc}", "equipe": equipe, "bairro": bairro, "vencimento": venc}


class TestDesignacoes(unittest.TestCase):
    def test_primeira_execucao_nao_avisa(self):
        novas, base = detectar_designacoes(None, [reg("1", "NI201")])
        self.assertEqual(novas, [])
        self.assertEqual(base, {"1": "NI201"})

    def test_nova_e_redesignada(self):
        base = {"1": "NI201", "2": "NI202"}
        atuais = [reg("1", "NI201"), reg("2", "ni203 "), reg("3", "NI201")]
        novas, nova_base = detectar_designacoes(base, atuais)
        self.assertEqual(sorted(r["tdc"] for r in novas), ["2", "3"])
        self.assertEqual(nova_base, {"1": "NI201", "2": "NI203", "3": "NI201"})

    def test_exportacao_vazia_mantem_base(self):
        base = {"1": "NI201"}
        novas, nova_base = detectar_designacoes(base, [])
        self.assertEqual(novas, [])
        self.assertEqual(nova_base, base)


class TestMontarAvisos(unittest.TestCase):
    def test_uma_linha_por_ordem_e_um_push_por_equipe(self):
        itens = [
            (reg("1", "NI201"), "designada", None),
            (reg("2", "NI201"), "designada", None),
            (reg("3", "NI202", 25), "vencer", "30min"),
            (reg("4", "NI202", -5), "vencida", "vencida"),
            (reg("5", "", 10), "vencer", "15min"),  # sem equipe: ignorada
        ]
        linhas, pushes = montar_avisos(itens)
        self.assertEqual([l["tdc"] for l in linhas], ["1", "2", "3", "4"])
        self.assertEqual(linhas[2]["titulo"], "Religa vence em até 30 min · TdC 3")
        self.assertEqual(len(pushes), 3)
        p_designadas = next(p for p in pushes if p["equipe"] == "NI201")
        self.assertIn("2 novas religas designadas para NI201", p_designadas["titulo"])
        self.assertIn("TdC 1", p_designadas["mensagem"])
        self.assertIn("TdC 2", p_designadas["mensagem"])
        p_vencer = next(p for p in pushes if p["tag"] == "argos-vencer-30min")
        self.assertEqual(p_vencer["prioridade"], 5)
        p_vencida = next(p for p in pushes if p["tag"].startswith("argos-vencida"))
        self.assertIn("VENCEU", p_vencida["titulo"])

    def test_disparos_por_ordem_dos_alertas(self):
        regs = [reg("A", "NI201", 50), reg("B", "NI202", 20), reg("C", "NI202", -1), reg("D", "NI201", 300)]
        avisos, _, disparos = calcular_disparos(regs, T0, None, antecedencias=[60, 30])
        self.assertEqual(len(avisos), 3)
        self.assertEqual(sorted((r["tdc"], n) for r, n in disparos), [("A", "60min"), ("B", "30min"), ("C", "vencida")])
        # Mesmo instante de novo: nada dispara.
        _, estado, _ = calcular_disparos(regs, T0, None, antecedencias=[60, 30])
        _, _, disparos = calcular_disparos(regs, T0, estado, antecedencias=[60, 30])
        self.assertEqual(disparos, [])


class TestPendentes(unittest.TestCase):
    def aviso(self, id_, tdc, equipe, minutos_desde_envio, titulo="t"):
        envio = (T0 - timedelta(minutes=minutos_desde_envio)).astimezone(TIMEZONE).isoformat()
        return {"id": id_, "tdc": tdc, "equipe": equipe, "ultimo_envio_em": envio, "titulo": titulo}

    def test_dispensa_e_reenvio(self):
        registros = [reg("1", "NI201"), reg("2", "NI202")]
        pendentes = [
            self.aviso(10, "1", "NI201", 12),        # reenviar
            self.aviso(11, "1", "NI201", 3),         # enviado há pouco: espera
            self.aviso(12, "2", "NI201", 30),        # redesignada para NI202: dispensa
            self.aviso(13, "9", "NI202", 30),        # finalizada (saiu do relatório): dispensa
            self.aviso(14, TDC_TESTE, "NI202", 30),  # teste: nunca dispensa
        ]
        dispensar, reenviar = classificar_pendentes(pendentes, registros, T0, intervalo_min=10)
        self.assertEqual(sorted(dispensar), [12, 13])
        self.assertEqual({e: [a["id"] for a in avs] for e, avs in reenviar.items()}, {"NI201": [10], "NI202": [14]})

    def test_lembrete(self):
        p = montar_lembrete("NI201", [{"titulo": "Religa VENCIDA · TdC 1"}, {"titulo": "Nova religa designada · TdC 2"}])
        self.assertEqual(p["titulo"], "ARGOS · 2 avisos sem confirmar")
        self.assertIn("TdC 1", p["mensagem"])
        self.assertEqual(p["prioridade"], 5)


if __name__ == "__main__":
    unittest.main()
