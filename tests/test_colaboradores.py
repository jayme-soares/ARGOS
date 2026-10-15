import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openpyxl import Workbook

from argos import colaboradores
from argos.contas_equipes import codigo_valido, email_da_equipe, equipes_da_planilha
from argos.contratos import NEGOCIACAO
from argos.planilha import ler_exportacao_campo
from tests.test_contratos import CABECALHO

LINHAS = [
    ("SUL", 21687, "083446", "ALBERTO ALMEIDA CARLOS", "CARLOS ALBERTO MENDONCA"),
    ("SERRANA", 11778, "077188", "SANTOS PEDRO", "PEDRO HENRIQUE DOS SANTOS"),
    ("CAMPOS", 12712, "079494", "MESSIAS FELIX", "MESSIAS FELIX PEREIRA"),
    ("CAMPOS", 12712, "079494", "MESSIAS FELIX", "MESSIAS FELIX PEREIRA"),           # repetida
    ("ITAPERUNA", 14747, "079471_653", "CRISTIANO SOUZA NUNES LUCAS", "LUCAS CRISTIANO SOUZA NUNES"),
    ("ARARUAMA", 14221, "074122", "DOS SANTOS VALADARES WANDERSON", "WANDERSON DOS SANTOS VALADARES"),
    ("ARARUAMA", 15227, "074122", "DOS SANTOS VALADARES WANDERSON", "ANDERSON BARBOZA COUTINHO"),
    ("SERRANA", 18472, "077189", "VINICIUS DA COSTA MARCUS", "MARCUS VINICIUS DA COSTA"),
    ("PADUA", 11792, "077189", "VINICIUS DA COSTA MARCUS", "MARCUS VINICIUS PACHECO FERREIRA"),
    ("MANGARATIBA", 20045, None, "ALAN PIRES", "ALAN PIRES ALVES"),
]


class TestColaboradores(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        wb = Workbook()
        ws = wb.active
        ws.title = "Colaboradores"
        ws.append(["REGIONAL", "MATRÍCULA", "Codigo Equipe", "ACESSO EORDER", "REFUN", None])
        for linha in LINHAS:
            ws.append(list(linha))
        self.xlsx = self.dir / "colaboradores.xlsx"
        wb.save(self.xlsx)
        self.cadastro = colaboradores.ler_planilha(self.xlsx)

    def tearDown(self):
        self.tmp.cleanup()

    def test_le_planilha(self):
        self.assertEqual(len(self.cadastro), len(LINHAS) - 1)  # a repetida conta uma vez
        lucas = next(c for c in self.cadastro if c.matricula == "14747")
        self.assertEqual((lucas.codigo, lucas.nome, lucas.regional), ("79471", "LUCAS CRISTIANO SOUZA NUNES", "ITAPERUNA"))

    def test_matricula_pelo_codigo(self):
        m = lambda cod, nome=None: colaboradores.matricula(cod, nome, self.cadastro)
        self.assertEqual(m("083446", "ALBERTO MENDONÇA CARLOS"), "21687")  # nome diferente não importa
        self.assertEqual(m("77188"), "11778")                              # Excel sem o zero
        self.assertEqual(m("079494"), "12712")                             # linha repetida
        self.assertEqual(m("079471"), "14747")                             # sufixo _653
        self.assertEqual(m("074122", "DOS SANTOS VALADARES WANDERSON"), "14221")  # desempate pelo nome
        self.assertIsNone(m("074122"))
        self.assertIsNone(m("077189", "MARCUS SILVA"))                     # continua ambíguo
        self.assertIsNone(m("999999", "FULANO"))
        self.assertIsNone(m("", "ALAN PIRES"))

    def test_contas_e_avisos(self):
        with mock.patch("argos.contas_equipes.registros_do_estado", return_value=[
            {"codigo_equipe": "077189", "equipe_nome": "MARCUS SILVA"},
            {"codigo_equipe": "083446", "equipe_nome": "ALBERTO MENDONÇA CARLOS"},
        ]):
            equipes, avisos = equipes_da_planilha(self.xlsx)
        self.assertEqual(len(equipes), 9)
        self.assertEqual(equipes["14747"], "LUCAS CRISTIANO SOUZA NUNES")
        texto = "\n".join(avisos)
        self.assertIn("sem Codigo Equipe: 20045", texto)
        self.assertIn("código 074122 em mais de uma matrícula", texto)
        self.assertIn("código 077189 em mais de uma matrícula", texto)
        self.assertIn("equipe do eOrder sem matrícula: MARCUS SILVA", texto)
        self.assertNotIn("ALBERTO", texto)

    def test_matricula_valida(self):
        self.assertEqual(colaboradores.normalizar_matricula(20045.0), "20045")
        self.assertEqual(colaboradores.normalizar_matricula(" 020045 "), "20045")
        self.assertTrue(codigo_valido("4279", NEGOCIACAO))
        self.assertFalse(codigo_valido("CENEGED-ANGRA-01", NEGOCIACAO))
        self.assertEqual(email_da_equipe("20045", NEGOCIACAO), "neg-20045@equipes.argos.local")

    def test_bot_usa_a_matricula_como_equipe(self):
        wb = Workbook()
        ws = wb.active
        ws.title = "TdC"
        ws.append(CABECALHO)
        ws.append([1, "A1", "RELIGA", 1, "ANGRA DOS REIS", "Centro", "083446", "ALBERTO MENDONÇA CARLOS",
                   "30/09/2026 12:00", "Enviado ao Campo"])
        ws.append([2, "A2", "RELIGA", 2, "PETROPOLIS", "Centro", "077189", "MARCUS SILVA",
                   "30/09/2026 13:00", "Enviado ao Campo"])
        exportacao = self.dir / "neg.xlsx"
        wb.save(exportacao)
        with mock.patch.dict(os.environ, {"ARGOS_NEG_COLABORADORES": str(self.xlsx)}):
            regs = ler_exportacao_campo(exportacao, NEGOCIACAO)["em_aberto"]
        self.assertEqual([(r["equipe"], r["equipe_nome"]) for r in regs],
                         [("21687", "ALBERTO MENDONÇA CARLOS"), ("MARCUS-SILVA", "MARCUS SILVA")])


if __name__ == "__main__":
    unittest.main()
