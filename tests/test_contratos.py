import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook

from argos.alertas import calcular_alertas
from argos.avisos_equipe import montar_avisos
from argos.config import TIMEZONE
from argos.contratos import NEGOCIACAO, SOC, parse_programaveis, slug_equipe
from argos.planilha import ler_exportacao_campo
from argos.workers import MonitorCampo, _CampoContrato

CABECALHO = ["Código TdC", "Numero de Serviço", "Tipo de Serviço", "Código Cliente", "Município",
             "Bairro", "Código Equipe", "Equipe", "Prazo ANS Legal", "Estado TdC"]


class TestContratos(unittest.TestCase):
    def test_slug_equipe(self):
        self.assertEqual(slug_equipe("Ceneged Angra 01 - João"), "CENEGED-ANGRA-01-JOAO")
        self.assertEqual(slug_equipe("  macaé/02 "), "MACAE-02")
        self.assertEqual(slug_equipe(None), "")

    def test_chave_equipe_por_contrato(self):
        self.assertEqual(SOC.chave_equipe("ni2 01lrp-b"), "NI201LRP-B")
        self.assertEqual(NEGOCIACAO.chave_equipe("Ceneged Angra 01"), "CENEGED-ANGRA-01")

    def test_parse_programaveis(self):
        cos = parse_programaveis(" 140:52300:Angra ; 141:52301;;sem_busca; :1")
        self.assertEqual([(c.value, c.busca, c.nome) for c in cos], [("140", "52300", "Angra"), ("141", "52301", "141")])

    def test_estado_separado(self):
        self.assertEqual(SOC.arquivo("alertas.json"), "alertas.json")
        self.assertEqual(NEGOCIACAO.arquivo("alertas.json"), "negociacao/alertas.json")
        self.assertEqual(SOC.worker("programaveis"), "programaveis")
        self.assertEqual(NEGOCIACAO.worker("programaveis"), "programaveis_negociacao")
        self.assertNotEqual(SOC.snapshot_key, NEGOCIACAO.snapshot_key)

    def test_marca_nos_alertas(self):
        agora = datetime(2026, 9, 29, 10, 0, tzinfo=TIMEZONE)
        avisos, _ = calcular_alertas([{"tdc": "1", "vencimento": "2026-09-29T10:20-03:00"}], agora, None,
                                     marca=NEGOCIACAO.marca)
        self.assertTrue(avisos[0]["titulo"].startswith("ARGOS Negociação · "))

    def test_avisos_levam_contrato(self):
        linhas, _ = montar_avisos([({"tdc": "1", "equipe": "CENEGED-ANGRA-01"}, "designada", None)], "negociacao")
        self.assertEqual(linhas[0]["contrato"], "negociacao")
        self.assertEqual(linhas[0]["equipe"], "CENEGED-ANGRA-01")


class TestPlanilhaNegociacao(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def planilha(self, linhas, nome="neg.xlsx"):
        wb = Workbook()
        ws = wb.active
        ws.title = "TdC"
        ws.append(CABECALHO)
        for linha in linhas:
            ws.append(linha)
        caminho = self.dir / nome
        wb.save(caminho)
        return caminho

    def test_equipe_pelo_nome_sem_filtro_de_municipio(self):
        caminho = self.planilha([
            [1, "A1", "RELIGA", 1, "ANGRA DOS REIS", "Centro", "084941", "Ceneged Angra 01", "30/09/2026 12:00", "Enviado ao Campo"],
            [2, "A2", "RELIGA", 2, "PARATY", "Centro", "", "Ceneged Angra 02", "30/09/2026 11:00", "Enviado ao Campo"],
            [3, "A3", "RELIGA", 3, "ANGRA DOS REIS", "Centro", "084941", "", "30/09/2026 11:00", "Enviado ao Campo"],
        ])
        dados = ler_exportacao_campo(caminho, NEGOCIACAO, "Ceneged - Angra (CENEGED_ANGRA)")
        regs = dados["em_aberto"]
        self.assertEqual([r["tdc"] for r in regs], ["2", "1"])  # sem equipe fica de fora
        self.assertEqual(regs[1]["equipe"], "CENEGED-ANGRA-01")
        self.assertEqual(regs[1]["equipe_nome"], "Ceneged Angra 01")
        self.assertEqual(regs[1]["co"], "Ceneged - Angra (CENEGED_ANGRA)")

    def test_soc_continua_filtrando(self):
        caminho = self.planilha([
            [1, "A1", "RELIGA", 1, "MARICA", "Centro", "NI201LRP-B", "Fulano", "30/09/2026 12:00", "Enviado ao Campo"],
            [2, "A2", "RELIGA", 2, "ANGRA DOS REIS", "Centro", "NI201LRP-B", "Fulano", "30/09/2026 12:00", "Enviado ao Campo"],
        ])
        regs = ler_exportacao_campo(caminho)["em_aberto"]
        self.assertEqual([(r["tdc"], r["equipe"]) for r in regs], [("1", "NI201LRP-B")])
        self.assertNotIn("co", regs[0])

    def test_co_que_falha_mantem_dados_anteriores(self):
        monitor = MonitorCampo((NEGOCIACAO,)) if NEGOCIACAO.cos_campo else None
        if monitor is None:
            self.skipTest("sem COs de campo configurados")
        angra, araruama = NEGOCIACAO.cos_campo[:2]
        d = _CampoContrato(NEGOCIACAO)
        d.registros = [{"tdc": "9", "co": araruama, "vencimento": None, "equipe": "X"},
                       {"tdc": "8", "co": angra, "vencimento": None, "equipe": "X"}]
        d.finalizadas = []
        caminho = self.planilha([
            [1, "A1", "RELIGA", 1, "ANGRA DOS REIS", "Centro", "", "Ceneged Angra 01", "30/09/2026 12:00", "Enviado ao Campo"],
        ])
        agora = datetime(2026, 9, 30, 9, 0, tzinfo=TIMEZONE)
        planilhas = {co: RuntimeError("falhou") for co in NEGOCIACAO.cos_campo}
        planilhas[angra] = caminho
        self.assertTrue(monitor._ler_contrato(d, planilhas, agora))
        # Angra veio da planilha nova; Araruama falhou e manteve o anterior.
        self.assertEqual(sorted(r["tdc"] for r in d.registros), ["1", "9"])
        self.assertIn(araruama, d.cos_com_erro)
        self.assertEqual(d.atualizado_em, agora)

        d2 = _CampoContrato(NEGOCIACAO)
        d2.registros = [{"tdc": "7"}]
        self.assertFalse(monitor._ler_contrato(d2, {co: RuntimeError("x") for co in NEGOCIACAO.cos_campo}, agora))
        self.assertEqual(d2.registros, [{"tdc": "7"}])


if __name__ == "__main__":
    unittest.main()
