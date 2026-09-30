"""Configuração do ARGOS. Tudo que pode mudar entre ambientes vem de variável
de ambiente (ver .env.example); o resto são constantes do eOrder mapeadas na
investigação das telas."""

import os
from pathlib import Path
from zoneinfo import ZoneInfo


def _corrigir_mojibake(texto: str) -> str:
    """Conserta UTF-8 lido como cp1252/latin-1 ("MARICÃ\\x81" -> "MARICÁ").
    Acontece ao carregar o .env no PowerShell sem -Encoding UTF8."""
    if "Ã" not in texto and "Â" not in texto:
        return texto
    for codificacao in ("cp1252", "latin-1"):
        try:
            return texto.encode(codificacao).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    return texto


def _env_str(nome: str, padrao: str | None = None) -> str | None:
    valor = os.environ.get(nome)
    if valor is None or valor.strip() == "":
        return padrao
    return _corrigir_mojibake(valor.strip())


def _env_int(nome: str, padrao: int) -> int:
    valor = _env_str(nome)
    return int(valor) if valor is not None else padrao


def _env_bool(nome: str, padrao: bool) -> bool:
    valor = _env_str(nome)
    if valor is None:
        return padrao
    return valor.lower() in ("1", "true", "sim", "yes", "on")


RAIZ_PROJETO = Path(__file__).resolve().parent.parent

# ------------------------------------------------------------------
# PASTAS DE DADOS
# ------------------------------------------------------------------
# No Docker aponta para /data, um volume persistente. Sem ele, um restart do
# container apagaria o estado e o bot trataria tudo como novo de novo (era o
# problema do deploy antigo no Render, que não tinha disco persistente).
DATA_DIR = Path(_env_str("ARGOS_DATA_DIR", str(RAIZ_PROJETO / "data")))
DOWNLOAD_DIR = DATA_DIR / "downloads"
DEBUG_DIR = DATA_DIR / "debug"

# ------------------------------------------------------------------
# eOrder
# ------------------------------------------------------------------
BASE_URL = "https://eorder-rio.enel.com/geocallamp/w/Servlet"

EORDER_USER = _env_str("EORDER_USER")
EORDER_PASS = _env_str("EORDER_PASS")

WAIT_TIMEOUT = 20

# Tempo máximo que um único comando Selenium pode levar antes de virar
# exceção. Ver comentário em eorder/driver.py.
COMANDO_TIMEOUT_SEGUNDOS = 90

# Chrome headless por padrão. ARGOS_HEADLESS=0 abre a janela (útil para
# acompanhar a exportação rodando localmente).
CHROME_HEADLESS = _env_bool("ARGOS_HEADLESS", True)
# Flags de baixo consumo de memória herdadas do deploy no Render (512MB).
# Na VPS dá para desligar com ARGOS_CHROME_BAIXA_MEMORIA=0 se a grade da
# Busca TdC ficar pesada demais para o limite de heap do JS.
CHROME_BAIXA_MEMORIA = _env_bool("ARGOS_CHROME_BAIXA_MEMORIA", True)

# --- Programáveis (Plano Diário) ---
# "Centro Operativo" do painel geral do Plano Diário. Passou a ser
# obrigatório/vir vazio no eOrder — sem ele a busca geral não roda e a
# faixa de abas (Atividades Programáveis etc.) nunca aparece.
CENTRO_OPERATIVO_VALUE = "132"  # Valtellina - Niterói
# value da <option> "CENEGED RELIGAÇÃO MARICA" — as opções têm espaços de
# padding no texto, por isso select_by_visible_text falha; o value é exato.
SAVED_SEARCH_VALUE = "52224"

# Quantos dias úteis à frente de hoje usar no filtro "Prazo ANS Legal".
# 1 = próximo dia útil (regra de negócio real). Está em 2 porque com 1 ainda
# não havia dados no ambiente de teste.
DIAS_UTEIS_PRAZO = _env_int("ARGOS_DIAS_UTEIS_PRAZO", 2)

INTERVALO_PROGRAMAVEIS_SEGUNDOS = _env_int("ARGOS_INTERVALO_PROGRAMAVEIS_SEG", 60)

# --- Em campo (Busca TdC) ---
FILTRO_CAMPO = _env_str("ARGOS_FILTRO_CAMPO", "PARCIAL RELIGA CENEGED - MARICÁ")
# O filtro salvo traz ordens de outros municípios e de equipes de outras
# empresas; só ficam as do município abaixo (comparado sem acento/caixa) e
# as de equipes cujo "Código Equipe" começa com o prefixo (NI2 = CENEGED).
MUNICIPIO_CAMPO = _env_str("ARGOS_MUNICIPIO_CAMPO", "MARICÁ")
PREFIXO_EQUIPE_CAMPO = _env_str("ARGOS_PREFIXO_EQUIPE_CAMPO", "NI2")
INTERVALO_CAMPO_MINUTOS = _env_int("ARGOS_INTERVALO_CAMPO_MIN", 30)
# Se a exportação falhar, tenta de novo depois desse tempo (em vez de
# esperar o intervalo cheio de 30 min).
RETENTATIVA_CAMPO_MINUTOS = _env_int("ARGOS_RETENTATIVA_CAMPO_MIN", 5)
# A Busca TdC exige "Data de lançamento": de N dias atrás até hoje.
CAMPO_DIAS_ATRAS = _env_int("ARGOS_CAMPO_DIAS_ATRAS", 7)
# IDs dos inputs "Data de lançamento" (início/fim) mapeados na tela. O GWT
# pode gerar IDs diferentes em outra versão da tela — por isso ficam aqui e
# há fallback por XPath em busca_tdc.py.
ID_DATA_LANCAMENTO_INICIO = _env_str("ARGOS_ID_DATA_LANC_INICIO", "698246")
ID_DATA_LANCAMENTO_FIM = _env_str("ARGOS_ID_DATA_LANC_FIM", "698247")
# A exportação é assíncrona no eOrder: o arquivo aparece na "Lista de
# exportação" depois de um tempo. Limite total de espera e intervalo entre
# cada "Atualizar" da lista.
EXPORTACAO_TIMEOUT_SEGUNDOS = _env_int("ARGOS_EXPORTACAO_TIMEOUT_SEG", 15 * 60)
EXPORTACAO_POLLING_SEGUNDOS = _env_int("ARGOS_EXPORTACAO_POLLING_SEG", 30)

# --- Alertas de vencimento ---
# Antecedências (em minutos) dos avisos de "vai vencer". Cada ordem recebe
# cada aviso uma única vez.
ALERTAS_MINUTOS = sorted(
    {int(x) for x in (_env_str("ARGOS_ALERTAS_MIN", "60,30") or "").split(",") if x.strip()},
    reverse=True,
)
LEMBRETE_VENCIDAS_MINUTOS = _env_int("ARGOS_LEMBRETE_VENCIDAS_MIN", 60)
# Entre uma exportação e outra (30 min), os alertas são reavaliados sobre os
# últimos dados lidos — senão o aviso de 30 min poderia chegar até 30 min
# atrasado.
REAVALIACAO_ALERTAS_MINUTOS = _env_int("ARGOS_REAVALIACAO_ALERTAS_MIN", 5)
# Não dispara alerta de vencimento em cima de dados velhos (exportação
# falhando há muito tempo): a ordem pode já ter sido finalizada.
IDADE_MAXIMA_DADOS_CAMPO_MINUTOS = _env_int("ARGOS_IDADE_MAX_CAMPO_MIN", 2 * INTERVALO_CAMPO_MINUTOS)

# ------------------------------------------------------------------
# JANELA DE FUNCIONAMENTO
# ------------------------------------------------------------------
# Fora da janela o Chrome fica desligado e nenhuma checagem/alerta acontece.
TIMEZONE = ZoneInfo("America/Sao_Paulo")
HORARIO_INICIO = _env_int("ARGOS_HORARIO_INICIO", 7)   # inclusive
HORARIO_FIM = _env_int("ARGOS_HORARIO_FIM", 20)        # exclusive
# 0=segunda ... 6=domingo. Padrão seg-sex.
DIAS_SEMANA = {int(x) for x in (_env_str("ARGOS_DIAS_SEMANA", "0,1,2,3,4") or "").split(",") if x.strip()}
FORA_DA_JANELA_INTERVALO_SEGUNDOS = 300

# ------------------------------------------------------------------
# ROBUSTEZ
# ------------------------------------------------------------------
MAX_FALHAS_CONSECUTIVAS_ANTES_DE_RECUPERAR = 3
# Se algum monitor passar esse tempo sem progresso, o processo se mata
# (os._exit) e o Docker (restart: unless-stopped) sobe um container novo —
# só um processo novo resolve uma sessão de chromedriver travada de vez.
WATCHDOG_SEM_PROGRESSO_SEGUNDOS = 8 * 60

# Se o eOrder não aceitar duas sessões simultâneas do mesmo usuário, liga
# este modo: os monitores passam a revezar o acesso ao eOrder (ver
# workers.py).
MODO_SEQUENCIAL = _env_bool("ARGOS_MODO_SEQUENCIAL", False)

# ------------------------------------------------------------------
# NOTIFICAÇÃO PUSH (ntfy)
# ------------------------------------------------------------------
# No ntfy.sh público o nome do tópico funciona como senha por obscuridade —
# use um sufixo aleatório e não divulgue.
NTFY_SERVER = _env_str("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = _env_str("NTFY_TOPIC")
HABILITAR_NOTIFICACAO_PUSH = _env_bool("ARGOS_PUSH", True)
# Link aberto ao tocar na notificação (URL do painel no Vercel).
PAINEL_URL = _env_str("ARGOS_PAINEL_URL")

# ------------------------------------------------------------------
# PUBLICAÇÃO DO SNAPSHOT (Upstash Redis)
# ------------------------------------------------------------------
UPSTASH_REDIS_REST_URL = _env_str("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = _env_str("UPSTASH_REDIS_REST_TOKEN")
SNAPSHOT_KEY = _env_str("ARGOS_SNAPSHOT_KEY", "argos:snapshot")

# Health-check HTTP local (não precisa ser exposto fora do container).
PORTA_SAUDE = _env_int("PORT", 8080)


def validar_config():
    """Falha cedo, com mensagem clara, se faltar configuração obrigatória."""
    faltando = [n for n, v in (("EORDER_USER", EORDER_USER), ("EORDER_PASS", EORDER_PASS)) if not v]
    if faltando:
        raise RuntimeError(
            f"Variáveis de ambiente obrigatórias não definidas: {', '.join(faltando)}. "
            "Defina-as no .env — nunca deixe credenciais hardcoded no código."
        )
    avisos = []
    if HABILITAR_NOTIFICACAO_PUSH and not NTFY_TOPIC:
        avisos.append("NTFY_TOPIC não definido — notificações push desativadas.")
    if not (UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN):
        avisos.append("UPSTASH_REDIS_REST_URL/TOKEN não definidos — o painel não vai receber dados.")
    return avisos
