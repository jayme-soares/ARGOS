"""Contratos acompanhados pelo ARGOS: SOC e Negociação.

A lógica é a mesma nos dois (programáveis do Plano Diário + exportação da
Busca TdC); o que muda é o escopo:

- SOC: CO "Valtellina - Niterói", só o município de Maricá e as equipes cujo
  "Código Equipe" começa com NI2. Estado no DATA_DIR raiz (como sempre foi),
  snapshot em ARGOS_SNAPSHOT_KEY.
- Negociação: os COs "Ceneged - X (CENEGED_X)", cada um com a própria busca
  salva nas programáveis. Equipe pela coluna "Equipe" (o nome), sem filtro
  de município nem de prefixo. Estado em DATA_DIR/negociacao/, snapshot em
  "<ARGOS_SNAPSHOT_KEY>:negociacao".

Cada contrato tem snapshot, estado dos alertas, base de designações, tópico
ntfy e destinatários do Web Push próprios.
"""

import re
import unicodedata
from dataclasses import dataclass

from argos import config


@dataclass(frozen=True)
class CoProgramaveis:
    value: str          # <option value> do Centro Operativo no Plano Diário
    busca: str          # <option value> da busca salva em Atividades Programáveis
    nome: str = ""      # rótulo para log/painel


@dataclass(frozen=True)
class Contrato:
    id: str
    nome: str
    marca: str                          # início do título dos pushes
    snapshot_key: str
    subpasta: str                       # dentro do DATA_DIR ("" = raiz)
    ntfy_topic: str | None
    programaveis: tuple[CoProgramaveis, ...]
    municipio_programaveis: str | None  # filtro pelo fim do endereço; None = todos
    cos_campo: tuple[str, ...]          # texto do CO no combo da Busca TdC
    municipio_campo: str | None
    coluna_equipe: str
    prefixo_equipe: str | None
    # SOC: código sem espaços. Negociação: nome virando slug (ver chave_equipe).
    equipe_por_nome: bool = False

    def arquivo(self, nome: str) -> str:
        return f"{self.subpasta}{nome}"

    def worker(self, base: str) -> str:
        """Nome do monitor no watchdog/log: "programaveis", "programaveis_negociacao"."""
        return base if self.id == "soc" else f"{base}_{self.id}"

    def chave_equipe(self, valor) -> str:
        return slug_equipe(valor) if self.equipe_por_nome else re.sub(r"\s+", "", str(valor or "")).upper()


def slug_equipe(nome) -> str:
    """"Ceneged Angra 01 - João" -> "CENEGED-ANGRA-01-JOAO". Vira o código de
    login da equipe da Negociação e a chave da equipe nos avisos. Precisa
    bater com slugEquipe() em web/api/_equipes.js."""
    sem_acento = "".join(c for c in unicodedata.normalize("NFKD", str(nome or "")) if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9]+", "-", sem_acento.upper()).strip("-")


def parse_programaveis(texto: str | None) -> tuple[CoProgramaveis, ...]:
    """"140:52300:Angra; 141:52301" -> CoProgramaveis. O nome é opcional."""
    cos = []
    for item in (texto or "").split(";"):
        partes = [p.strip() for p in item.split(":", 2)]
        if len(partes) < 2 or not partes[0] or not partes[1]:
            continue
        cos.append(CoProgramaveis(value=partes[0], busca=partes[1], nome=partes[2] if len(partes) > 2 else partes[0]))
    return tuple(cos)


def _lista(texto: str | None) -> tuple[str, ...]:
    return tuple(x.strip() for x in (texto or "").split(";") if x.strip())


SOC = Contrato(
    id="soc",
    nome="SOC",
    marca="ARGOS",
    snapshot_key=config.SNAPSHOT_KEY,
    subpasta="",
    ntfy_topic=config.NTFY_TOPIC,
    programaveis=(CoProgramaveis(config.CENTRO_OPERATIVO_VALUE, config.SAVED_SEARCH_VALUE, "Valtellina - Niterói"),),
    municipio_programaveis=config.MUNICIPIO_CAMPO,
    cos_campo=(config.SOC_CO_CAMPO,),
    municipio_campo=config.MUNICIPIO_CAMPO,
    coluna_equipe="Código Equipe",
    prefixo_equipe=config.PREFIXO_EQUIPE_CAMPO,
)

NEGOCIACAO = Contrato(
    id="negociacao",
    nome="Negociação",
    marca="ARGOS Negociação",
    snapshot_key=f"{config.SNAPSHOT_KEY}:negociacao",
    subpasta="negociacao/",
    ntfy_topic=config.NTFY_TOPIC_NEGOCIACAO,
    programaveis=parse_programaveis(config.NEG_PROGRAMAVEIS),
    municipio_programaveis=None,
    cos_campo=_lista(config.NEG_COS_CAMPO),
    municipio_campo=None,
    coluna_equipe="Equipe",
    prefixo_equipe=None,
    equipe_por_nome=True,
)

TODOS = (SOC, NEGOCIACAO)
POR_ID = {c.id: c for c in TODOS}


def ativos() -> tuple[Contrato, ...]:
    return (SOC, NEGOCIACAO) if config.NEGOCIACAO else (SOC,)
