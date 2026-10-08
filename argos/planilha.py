"""Leitura da planilha exportada pela Busca TdC (religas em campo).

O "Exportar em xls" do eOrder não garante o formato do arquivo: pode vir
xlsx, xls binário, xlsb, HTML ou SpreadsheetML (XML) com extensão .xls, ou
compactado em zip. Por isso o formato é detectado pelo conteúdo, não pela
extensão.

Uso avulso:  python -m argos.planilha caminho/da/planilha.xls
"""

import io
import re
import sys
import warnings
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree

import pandas as pd

# Os exports do eOrder vêm sem estilo padrão; o openpyxl avisa a cada leitura.
warnings.filterwarnings("ignore", message="Workbook contains no default style")

from argos import config
from argos.eorder.ui import normalizar_texto

ABA_PREFERIDA = "TdC"
COLUNA_CHAVE = "Código TdC"

# campo do ARGOS -> nome da coluna na planilha (comparado sem acento/caixa/espaços extras)
COLUNAS = {
    "ordem": "Numero de Serviço",
    "tdc": "Código TdC",
    "cliente": "Código Cliente",
    # "Código Equipe" em vez de "Equipe": na coluna Equipe o eOrder às vezes
    # traz o nome do responsável no lugar do código da equipe.
    "equipe": "Código Equipe",
    "municipio": "Município",
    "bairro": "Bairro",
    "tipo": "Tipo de Serviço",
    "vencimento": "Prazo ANS Legal",
}
# Não obrigatórias: aparecem no painel se existirem.
COLUNAS_OPCIONAIS = {
    "endereco": "Endereço Completo",
    "estado_tdc": "Estado TdC",
    "nome_cliente": "Nome e Sobrenome Cliente",
}

# Aba com uma linha por operação do TdC: dá a hora em que a ordem foi
# finalizada (maior "Data Fim") e o resultado.
ABA_LINHAS = "Linhas TdC"
COLUNAS_LINHAS = {
    "tdc": "Código TdC",
    "data_fim": "Data Fim",
    "resultado": "Resultado",
    "causa": "Causa/Descritivo Resultado",
}

FORMATOS_DATA = ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")


# ------------------------------------------------------------------
# DETECÇÃO DE FORMATO / LEITURA BRUTA
# ------------------------------------------------------------------

def _ler_bruto(conteudo: bytes, nome: str) -> dict[str, pd.DataFrame]:
    """Retorna {nome_aba: DataFrame sem cabeçalho (header=None)}."""
    if conteudo[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(conteudo)) as z:
            nomes = z.namelist()
            if "xl/workbook.xml" in nomes:
                return pd.read_excel(io.BytesIO(conteudo), sheet_name=None, header=None, engine="openpyxl")
            if "xl/workbook.bin" in nomes:
                return pd.read_excel(io.BytesIO(conteudo), sheet_name=None, header=None, engine="pyxlsb")
            # zip "comum" com a planilha dentro
            internos = [n for n in nomes if not n.endswith("/")]
            if not internos:
                raise ValueError(f"{nome}: zip vazio.")
            interno = max(internos, key=lambda n: z.getinfo(n).file_size)
            return _ler_bruto(z.read(interno), interno)

    if conteudo[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return pd.read_excel(io.BytesIO(conteudo), sheet_name=None, header=None, engine="xlrd")

    texto = _decodificar(conteudo)
    inicio = texto.lstrip()[:2000].lower()
    if "urn:schemas-microsoft-com:office:spreadsheet" in inicio or "<workbook" in inicio:
        return _ler_spreadsheetml(texto)
    if inicio.startswith("<") or "<table" in inicio:
        tabelas = pd.read_html(io.StringIO(texto), header=None)
        return {f"tabela{i}": _cabecalho_html_como_linha(t) for i, t in enumerate(tabelas)}

    # Último recurso: CSV/TSV
    sep = ";" if texto.count(";") > texto.count(",") else ","
    if texto.count("\t") > texto.count(sep):
        sep = "\t"
    return {"csv": pd.read_csv(io.StringIO(texto), sep=sep, header=None, dtype=str)}


def _cabecalho_html_como_linha(df: pd.DataFrame) -> pd.DataFrame:
    """read_html usa células <th> como cabeçalho mesmo com header=None;
    devolve esse cabeçalho como primeira linha para o resto do fluxo tratar
    todos os formatos igual (ver _localizar_cabecalho)."""
    if isinstance(df.columns, pd.RangeIndex):
        return df
    cols = [c[-1] if isinstance(c, tuple) else c for c in df.columns]
    corpo = df.copy()
    corpo.columns = range(len(cols))
    return pd.concat([pd.DataFrame([cols]), corpo], ignore_index=True)


def _decodificar(conteudo: bytes) -> str:
    for cod in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return conteudo.decode(cod)
        except UnicodeDecodeError:
            continue
    return conteudo.decode("latin-1", errors="replace")


def _ler_spreadsheetml(texto: str) -> dict[str, pd.DataFrame]:
    """XML Spreadsheet 2003 (o que muito sistema Java chama de "xls")."""
    ns = {"ss": "urn:schemas-microsoft-com:office:spreadsheet"}
    # Tira a declaração <?xml encoding=...?>: o texto já foi decodificado e
    # a declaração poderia contradizer o encoding real.
    raiz = ElementTree.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", texto))
    abas = {}
    for i, ws in enumerate(raiz.findall("ss:Worksheet", ns)):
        nome = ws.get(f"{{{ns['ss']}}}Name") or f"aba{i}"
        linhas = []
        for row in ws.findall("ss:Table/ss:Row", ns):
            valores = []
            for cell in row.findall("ss:Cell", ns):
                idx = cell.get(f"{{{ns['ss']}}}Index")
                if idx:
                    while len(valores) < int(idx) - 1:
                        valores.append(None)
                data = cell.find("ss:Data", ns)
                valores.append(data.text if data is not None else None)
            linhas.append(valores)
        abas[nome] = pd.DataFrame(linhas)
    return abas


# ------------------------------------------------------------------
# CABEÇALHO / COLUNAS
# ------------------------------------------------------------------

def _localizar_cabecalho(df: pd.DataFrame, max_linhas=15) -> int | None:
    """Índice da linha que contém 'Código TdC' (o export pode ter linhas de
    título/filtros antes do cabeçalho)."""
    alvo = normalizar_texto(COLUNA_CHAVE)
    for i in range(min(max_linhas, len(df))):
        if any(normalizar_texto(str(v)) == alvo for v in df.iloc[i].tolist() if v is not None):
            return i
    return None


def _com_cabecalho(df: pd.DataFrame) -> pd.DataFrame | None:
    i = _localizar_cabecalho(df)
    if i is None:
        return None
    cabecalho = [" ".join(str(c).split()) if c is not None and str(c) != "nan" else f"_col{j}"
                 for j, c in enumerate(df.iloc[i].tolist())]
    dados = df.iloc[i + 1:].copy()
    dados.columns = cabecalho
    return dados.dropna(how="all")


def _escolher_aba(abas: dict[str, pd.DataFrame]) -> pd.DataFrame:
    ordem = sorted(abas, key=lambda n: 0 if normalizar_texto(n) == normalizar_texto(ABA_PREFERIDA) else 1)
    for nome in ordem:
        df = _com_cabecalho(abas[nome])
        if df is not None:
            return df
    raise ValueError(
        f"Nenhuma aba com a coluna '{COLUNA_CHAVE}'. Abas encontradas: {list(abas)}"
    )


def _mapear_colunas(df: pd.DataFrame) -> dict[str, str]:
    """campo do ARGOS -> nome real da coluna no DataFrame. Se a mesma
    coluna aparecer duplicada (ex.: 'Equipe' em duas seções), fica a primeira."""
    por_normalizado = {}
    for col in df.columns:
        por_normalizado.setdefault(normalizar_texto(col), col)

    mapa, faltando = {}, []
    for campo, nome in COLUNAS.items():
        real = por_normalizado.get(normalizar_texto(nome))
        if real is None:
            faltando.append(nome)
        else:
            mapa[campo] = real
    if faltando:
        raise ValueError(
            f"Colunas obrigatórias ausentes na planilha: {faltando}. "
            f"Colunas disponíveis: {list(df.columns)}"
        )
    for campo, nome in COLUNAS_OPCIONAIS.items():
        real = por_normalizado.get(normalizar_texto(nome))
        if real is not None:
            mapa[campo] = real
    return mapa


# ------------------------------------------------------------------
# CONVERSÃO DE VALORES
# ------------------------------------------------------------------

def _texto(valor) -> str:
    """Códigos lidos como número pelo Excel viram '123.0' — volta para '123'."""
    if valor is None:
        return ""
    if isinstance(valor, float):
        if valor != valor:  # NaN
            return ""
        if valor.is_integer():
            return str(int(valor))
    texto = " ".join(str(valor).split())
    if re.fullmatch(r"\d+\.0", texto):
        texto = texto[:-2]
    return "" if texto.lower() in ("nan", "nat", "none") else texto


def converter_data(valor) -> datetime | None:
    """Aceita datetime, Timestamp, serial do Excel ou texto dd/mm/aaaa hh:mm.
    Retorna datetime com fuso de Brasília (o eOrder mostra hora local)."""
    if valor is None:
        return None
    if isinstance(valor, pd.Timestamp):
        if pd.isna(valor):
            return None
        valor = valor.to_pydatetime()
    if isinstance(valor, datetime):
        return valor if valor.tzinfo else valor.replace(tzinfo=config.TIMEZONE)
    if isinstance(valor, (int, float)):
        if valor != valor or valor <= 0:
            return None
        base = datetime(1899, 12, 30)  # serial do Excel
        return (base + timedelta(days=float(valor))).replace(microsecond=0, tzinfo=config.TIMEZONE)
    texto = _texto(valor)
    if not texto:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", texto):
        return converter_data(float(texto))
    for fmt in FORMATOS_DATA:
        try:
            return datetime.strptime(texto, fmt).replace(tzinfo=config.TIMEZONE)
        except ValueError:
            continue
    return None


# ------------------------------------------------------------------
# API
# ------------------------------------------------------------------

def _pertence_ao_escopo(municipio: str, equipe: str) -> bool:
    """Só o município e as equipes da CENEGED (ver config.MUNICIPIO_CAMPO e
    config.PREFIXO_EQUIPE_CAMPO). Ordem sem equipe fica de fora."""
    if normalizar_texto(municipio) != normalizar_texto(config.MUNICIPIO_CAMPO):
        return False
    return equipe.upper().startswith(config.PREFIXO_EQUIPE_CAMPO.upper())


def _eh_finalizada(estado_tdc: str) -> bool:
    estado = normalizar_texto(estado_tdc)
    return bool(estado) and any(estado.startswith(normalizar_texto(e)) for e in config.ESTADOS_FINALIZADOS)


def _ler_fim_por_tdc(abas: dict[str, pd.DataFrame]) -> dict[str, dict]:
    """Da aba "Linhas TdC" (uma linha por operação): por TdC, a maior "Data
    Fim" e o resultado dessa mesma linha. Aba ou colunas ausentes = {}."""
    df = next(
        (_com_cabecalho(d) for nome, d in abas.items() if normalizar_texto(nome) == normalizar_texto(ABA_LINHAS)),
        None,
    )
    if df is None:
        return {}
    por_normalizado = {}
    for col in df.columns:
        por_normalizado.setdefault(normalizar_texto(col), col)
    mapa = {campo: por_normalizado.get(normalizar_texto(nome)) for campo, nome in COLUNAS_LINHAS.items()}
    if mapa["tdc"] is None or mapa["data_fim"] is None:
        return {}

    fins: dict[str, dict] = {}
    for _, linha in df.iterrows():
        tdc = _texto(linha[mapa["tdc"]])
        data_fim = converter_data(linha[mapa["data_fim"]])
        if not tdc or data_fim is None:
            continue
        if tdc in fins and fins[tdc]["data_fim"] >= data_fim:
            continue
        fins[tdc] = {
            "data_fim": data_fim,
            "resultado": _texto(linha[mapa["resultado"]]) if mapa["resultado"] else "",
            "causa": _texto(linha[mapa["causa"]]) if mapa["causa"] else "",
        }
    return fins


def ler_exportacao_campo(caminho: Path | str) -> dict:
    """Separa as ordens do município/equipes do escopo em abertas e
    finalizadas (pelo "Estado TdC", ver config.ESTADOS_FINALIZADOS),
    deduplicadas por TdC.

    - em_aberto: ordenadas pelo vencimento (sem vencimento vão para o fim).
    - finalizadas: com `finalizada_em` (maior "Data Fim" da aba Linhas TdC;
      None se não houver), `resultado`, `causa` e `no_prazo`; ordenadas da
      mais recente para a mais antiga.
    - estados: contagem de TdCs por "Estado TdC" na planilha inteira, antes
      do filtro de escopo (para descobrir os nomes dos estados).

    Datas saem em ISO 8601 com fuso, prontas para o JSON do painel."""
    caminho = Path(caminho)
    abas = _ler_bruto(caminho.read_bytes(), caminho.name)
    df = _escolher_aba(abas)
    mapa = _mapear_colunas(df)

    vistos: set[str] = set()
    estados: dict[str, int] = {}
    em_aberto: list[dict] = []
    finalizadas: list[dict] = []
    fins = None
    for _, linha in df.iterrows():
        tdc = _texto(linha[mapa["tdc"]])
        if not tdc or tdc in vistos:
            continue
        vistos.add(tdc)
        estado_tdc = _texto(linha[mapa["estado_tdc"]]) if "estado_tdc" in mapa else ""
        estados[estado_tdc] = estados.get(estado_tdc, 0) + 1
        if not _pertence_ao_escopo(_texto(linha[mapa["municipio"]]), _texto(linha[mapa["equipe"]])):
            continue
        venc = converter_data(linha[mapa["vencimento"]])
        registro = {campo: _texto(linha[col]) for campo, col in mapa.items() if campo != "vencimento"}
        registro["vencimento"] = venc.isoformat(timespec="minutes") if venc else None
        if not _eh_finalizada(estado_tdc):
            em_aberto.append(registro)
            continue

        if fins is None:
            fins = _ler_fim_por_tdc(abas)
        fim = fins.get(tdc, {})
        data_fim = fim.get("data_fim")
        registro["finalizada_em"] = data_fim.isoformat(timespec="minutes") if data_fim else None
        registro["resultado"] = fim.get("resultado", "")
        registro["causa"] = fim.get("causa", "")
        registro["no_prazo"] = data_fim <= venc if data_fim and venc else None
        finalizadas.append(registro)

    em_aberto.sort(key=lambda r: (r["vencimento"] is None, r["vencimento"] or ""))
    finalizadas.sort(key=lambda r: r["finalizada_em"] or "", reverse=True)
    return {"em_aberto": em_aberto, "finalizadas": finalizadas, "estados": estados}


def ler_religas_em_campo(caminho: Path | str) -> list[dict]:
    """Só as ordens em aberto (ver ler_exportacao_campo)."""
    return ler_exportacao_campo(caminho)["em_aberto"]


def imprimir_tabela(registros: list[dict]):
    print(f"{len(registros)} ordem(ns) em campo")
    print(f"{'ORDEM':<12} {'TDC':<12} {'CLIENTE':<12} {'EQUIPE':<22} {'BAIRRO':<20} {'TIPO':<28} VENCIMENTO")
    for r in registros:
        venc = r["vencimento"][:16].replace("T", " ") if r["vencimento"] else "-"
        print(
            f"{r['ordem'][:12]:<12} {r['tdc'][:12]:<12} {r['cliente'][:12]:<12} {r['equipe'][:22]:<22} "
            f"{r['bairro'][:20]:<20} {r['tipo'][:28]:<28} {venc}"
        )


def imprimir_exportacao(dados: dict):
    """Ordens em aberto, finalizadas e a contagem por "Estado TdC" — serve
    para conferir quais estados entram em ARGOS_ESTADOS_FINALIZADOS."""
    print("Estado TdC (planilha inteira, antes do filtro de município/equipe):")
    for estado, n in sorted(dados["estados"].items(), key=lambda x: -x[1]):
        marca = "finalizada" if _eh_finalizada(estado) else "em aberto"
        print(f"  {n:>5}  {estado or '(vazio)':<40} -> {marca}")
    print()
    imprimir_tabela(dados["em_aberto"])
    print()
    print(f"{len(dados['finalizadas'])} ordem(ns) finalizada(s)")
    print(f"{'ORDEM':<12} {'TDC':<12} {'EQUIPE':<22} {'VENCIMENTO':<17} {'FINALIZADA EM':<17} {'PRAZO':<6} RESULTADO")
    for r in dados["finalizadas"]:
        venc = r["vencimento"][:16].replace("T", " ") if r["vencimento"] else "-"
        fim = r["finalizada_em"][:16].replace("T", " ") if r["finalizada_em"] else "-"
        prazo = {True: "ok", False: "fora", None: "-"}[r["no_prazo"]]
        print(f"{r['ordem'][:12]:<12} {r['tdc'][:12]:<12} {r['equipe'][:22]:<22} {venc:<17} {fim:<17} {prazo:<6} {r['resultado']}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Uso: python -m argos.planilha caminho/da/planilha")
        sys.exit(1)
    imprimir_exportacao(ler_exportacao_campo(sys.argv[1]))
