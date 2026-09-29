import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook

from argos.planilha import converter_data, ler_religas_em_campo

CABECALHO = [
    "CO", "Código TdC", "Numero de Serviço", " Tipo de Serviço", "Código Cliente",
    "Bairro", "Equipe", "Prazo ANS Legal", "Endereço Completo",
]
LINHAS = [
    ["132", 111222333, 9000001, "RELIGACAO URGENTE", 55501.0, "Centro", "EQ MARICA 01", "29/09/2026 14:30", "Rua A, 1"],
    ["132", 111222334, 9000002, "RELIGACAO NORMAL", 55502, "Itaipuaçu", "EQ MARICA 02", datetime(2026, 9, 29, 11, 5), "Rua B, 2"],
    ["132", 111222334, 9000002, "RELIGACAO NORMAL", 55502, "Itaipuaçu", "EQ MARICA 02", "29/09/2026 11:05", "duplicada"],
    ["132", 111222335, 9000003, "RELIGACAO NORMAL", 55503, "Inoã", "", None, ""],
]


class TestPlanilha(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def conferir(self, registros):
        self.assertEqual([r["tdc"] for r in registros], ["111222334", "111222333", "111222335"])
        primeiro = registros[0]
        self.assertEqual(primeiro["ordem"], "9000002")
        self.assertEqual(primeiro["cliente"], "55502")
        self.assertEqual(primeiro["tipo"], "RELIGACAO NORMAL")
        self.assertEqual(primeiro["equipe"], "EQ MARICA 02")
        self.assertEqual(primeiro["bairro"], "Itaipuaçu")
        self.assertTrue(primeiro["vencimento"].startswith("2026-09-29T11:05"))
        self.assertEqual(registros[1]["cliente"], "55501")
        self.assertIsNone(registros[2]["vencimento"])

    def test_xlsx_com_titulo_antes_do_cabecalho(self):
        wb = Workbook()
        wb.active.title = "Filtros"
        wb.active.append(["FILTROS"])
        ws = wb.create_sheet("TdC")
        ws.append(["Relatório exportado pelo eOrder"])
        ws.append([])
        ws.append(CABECALHO)
        for linha in LINHAS:
            ws.append(linha)
        caminho = self.dir / "export.xls"  # extensão enganosa de propósito
        wb.save(caminho)
        self.conferir(ler_religas_em_campo(caminho))

    def test_html_disfarcado_de_xls(self):
        def td(v):
            if isinstance(v, datetime):
                v = v.strftime("%d/%m/%Y %H:%M")
            return f"<td>{'' if v is None else v}</td>"
        html = "<html><body><table><tr>" + "".join(f"<th>{c}</th>" for c in CABECALHO) + "</tr>"
        for linha in LINHAS:
            html += "<tr>" + "".join(td(v) for v in linha) + "</tr>"
        html += "</table></body></html>"
        caminho = self.dir / "export.xls"
        caminho.write_text(html, encoding="utf-8")
        self.conferir(ler_religas_em_campo(caminho))

    def test_coluna_faltando_explica(self):
        wb = Workbook()
        wb.active.append(["Código TdC", "Equipe"])
        wb.active.append([1, "EQ"])
        caminho = self.dir / "x.xlsx"
        wb.save(caminho)
        with self.assertRaises(ValueError) as ctx:
            ler_religas_em_campo(caminho)
        self.assertIn("Prazo ANS Legal", str(ctx.exception))

    def test_converter_data(self):
        self.assertEqual(converter_data("29/09/2026 14:30").hour, 14)
        self.assertEqual(converter_data("29/09/2026 14:30:15").minute, 30)
        self.assertEqual(converter_data(46294.5).hour, 12)  # serial do Excel
        self.assertIsNone(converter_data(""))
        self.assertIsNone(converter_data(float("nan")))


if __name__ == "__main__":
    unittest.main()
