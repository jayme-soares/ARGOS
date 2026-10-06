import unittest

from argos.eorder.programaveis import filtrar_municipio, municipio_do_endereco


class TestFiltroMunicipio(unittest.TestCase):
    def test_municipio_do_endereco(self):
        self.assertEqual(municipio_do_endereco("RUA 90 00000 Q 513 LT 2 - ITAIPUACU, MARICA - RJ"), "MARICA")
        self.assertEqual(municipio_do_endereco("Rua Orestes Barbosa 500 LT 79 - , MARICA - RJ"), "MARICA")
        self.assertEqual(municipio_do_endereco("R 45 77 C 2 ENG MATO - ENGENHO DO MATO, NITEROI - RJ"), "NITEROI")
        self.assertEqual(municipio_do_endereco(None), "")

    def test_so_marica(self):
        registros = [
            {"codigo_tdc": "1", "endereco": "AVE B 00000 QD 117 - BOQUEIRAO, MARICA - RJ"},
            {"codigo_tdc": "2", "endereco": "RUA DAS GAIVOTAS 00000 LT 19 - PIRATININGA, NITEROI - RJ"},
            {"codigo_tdc": "3", "endereco": "Rua HUM 0 - INOA, Maricá - RJ"},
            {"codigo_tdc": "4", "endereco": ""},
        ]
        self.assertEqual([r["codigo_tdc"] for r in filtrar_municipio(registros)], ["1", "3"])


if __name__ == "__main__":
    unittest.main()
