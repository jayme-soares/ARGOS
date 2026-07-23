"""
Bot de monitoramento — Atividades Programáveis "CENEGED RELIGAÇÃO MARICA" (eOrder / OverIT)

O que este script faz:
1. Loga no eOrder (login precisa ser mapeado/ajustado - ver TODO abaixo)
2. Navega: Planejamento > Plano Diário > (painel Buscar) > aba "Atividades Programáveis"
3. Seleciona a busca salva "CENEGED RELIGAÇÃO MARICA"
4. Preenche "Prazo ANS Legal" com N dias úteis à frente às 16:00 (DIAS_UTEIS_PRAZO)
5. Executa a busca, extrai os registros reais da tabela (ignorando linhas de
   padding vazias que a grade sempre renderiza)
6. A cada INTERVALO_VERIFICACAO_SEGUNDOS, repete a busca e compara os
   códigos TdC extraídos com o snapshot da verificação anterior para saber
   quais são NOVOS e quais já estavam presentes.
7. Notificação: envia um push via ntfy.sh (chega no celular e no desktop de
   quem estiver inscrito no tópico NTFY_TOPIC) SOMENTE quando pelo menos uma
   religação NOVA entrou desde a checagem anterior — não repete aviso a cada
   ciclo só porque as mesmas religações continuam na página. O texto (também
   impresso no console todo ciclo) informa quantas são novas, quantas já
   estavam lá, o total, e o vencimento mais próximo considerando TODAS as
   religações atualmente na página.

Requisitos:
    pip install selenium requests

Este script é um ESQUELETO funcional baseado no mapeamento manual da tela.
Alguns seletores (login, ícone "buscar" da barra lateral) ainda precisam
ser confirmados/ajustados por não terem sido totalmente mapeados na sessão
de investigação (ver mapeamento_fluxo_religacao.md).
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException

# ------------------------------------------------------------------
# CONFIGURAÇÃO
# ------------------------------------------------------------------

BASE_URL = "https://eorder-rio.enel.com/geocallamp/w/Servlet"

# Credenciais: SEMPRE via variável de ambiente, nunca hardcoded no código.
#   - Local (PowerShell):  $env:EORDER_USER = "ENELINT\BR0177234757"
#                           $env:EORDER_PASS = "sua-senha"
#   - Local (bash):         export EORDER_USER='ENELINT\BR0177234757'
#                           export EORDER_PASS='sua-senha'
#   - GitHub Actions: cadastre EORDER_USER e EORDER_PASS em Settings >
#     Secrets and variables > Actions, e injete-os no step via `env:`.
EORDER_USER = os.environ.get("EORDER_USER")
EORDER_PASS = os.environ.get("EORDER_PASS")

if not EORDER_USER or not EORDER_PASS:
    raise RuntimeError(
        "Variáveis de ambiente EORDER_USER e/ou EORDER_PASS não definidas. "
        "Defina-as antes de rodar o script — nunca deixe credenciais hardcoded no código."
    )

SAVED_SEARCH_TEXT = "CENEGED RELIGAÇÃO MARICA"  # texto exibido no combobox (sem acento no sistema)
SAVED_SEARCH_VALUE = "52224"  # value da <option> — as opções têm espaços de padding no texto,
                               # por isso select_by_visible_text falha; usar o value é exato e estável

STATE_FILE = Path(__file__).parent / "seen_records.json"

WAIT_TIMEOUT = 20

# Quantos dias úteis à frente de hoje usar no filtro "Prazo ANS Legal".
# 1 = próximo dia útil (regra de negócio real). Temporariamente em 2 porque
# com 1 ainda não há dados no ambiente de teste. Volte para 1 quando não
# precisar mais do dia extra.
DIAS_UTEIS_PRAZO = 2

# Intervalo entre verificações, em segundos. Ajustável.
INTERVALO_VERIFICACAO_SEGUNDOS = 60

# Quantas falhas de ciclo seguidas tolerar antes de tentar recuperar
# renavegando (e, se isso também falhar, relogando do zero). Sem isso, um
# estado de página quebrado faz o bot repetir o mesmo erro para sempre em
# vez de se recuperar sozinho — inaceitável para um bot que roda contínuo.
MAX_FALHAS_CONSECUTIVAS_ANTES_DE_RECUPERAR = 3


# ------------------------------------------------------------------
# JANELA DE FUNCIONAMENTO (rodando na nuvem, plano free do Render)
# ------------------------------------------------------------------
# O bot só deve realizar login/checagens dentro do horário comercial,
# segunda a sexta, 07h-20h (fuso de Brasília). Fora dessa janela o
# navegador Selenium/Chrome fica DESLIGADO (economiza RAM/CPU, que são
# escassos no plano free) e o processo apenas fica reconferindo o relógio.
#
# Isso sozinho não impede o Render de "adormecer" o serviço: o plano free
# derruba o Web Service após 15 min sem receber tráfego HTTP, e o processo
# Python inteiro (inclusive esse loop) para junto. Por isso é necessário um
# "ping" HTTP externo e gratuito (ex.: cron-job.org) batendo no endpoint
# de health-check (ver iniciar_servidor_saude) a cada ~10 min, SOMENTE
# dentro dessa mesma janela seg-sex 07h-20h — fora dela deixamos o serviço
# dormir mesmo, que é o comportamento desejado. Ver README.md para o passo
# a passo de configuração desse ping.
TIMEZONE = ZoneInfo("America/Sao_Paulo")
HORARIO_INICIO = 7   # 07:00 (inclusive)
HORARIO_FIM = 20      # 20:00 (exclusive — para de checar às 20:00 em diante)
DIAS_UTEIS_SEMANA = range(0, 5)  # 0=segunda ... 4=sexta (weekday() do Python)

# Intervalo de "cochilo" enquanto FORA da janela permitida — não precisa ser
# curto, pois nenhuma checagem real acontece nesse estado.
FORA_DA_JANELA_INTERVALO_SEGUNDOS = 300


def dentro_da_janela_permitida(agora: datetime | None = None) -> bool:
    """True se `agora` (default: agora mesmo, fuso de Brasília) cai dentro
    da janela seg-sex HORARIO_INICIO–HORARIO_FIM em que o bot deve operar."""
    agora = agora or datetime.now(TIMEZONE)
    if agora.weekday() not in DIAS_UTEIS_SEMANA:
        return False
    return HORARIO_INICIO <= agora.hour < HORARIO_FIM


# ------------------------------------------------------------------
# NOTIFICAÇÃO PUSH (ntfy.sh) — celular e desktop
# ------------------------------------------------------------------
# ntfy.sh funciona por "tópico": qualquer pessoa que assinar esse tópico
# (app Android/iOS, navegador desktop ou app desktop do ntfy) recebe as
# notificações. Não precisa de conta nem cadastro — basta compartilhar o
# nome do tópico com quem deve receber. Instruções para os usuários finais
# estão no README/mensagem de setup (assinar NTFY_TOPIC abaixo).
#
# IMPORTANTE: no ntfy.sh público, o nome do tópico funciona como uma senha
# por obscuridade — quem descobrir/adivinhar o nome também recebe (e pode
# publicar) notificações nele. Troque o sufixo abaixo por algo próprio e
# não-óbvio, e não divulgue esse nome publicamente. Para mais garantia,
# hospede seu próprio servidor ntfy (https://docs.ntfy.sh/install/) e
# aponte NTFY_SERVER para ele.
NTFY_SERVER = "https://ntfy.sh"
NTFY_TOPIC = "ceneged-religacao-marica-x7k2p9"

# Liga/desliga o envio de push sem precisar comentar código (útil para
# testar o scraping isoladamente sem spammar o tópico).
HABILITAR_NOTIFICACAO_PUSH = True


# ------------------------------------------------------------------
# UTILITÁRIOS DE DATA
# ------------------------------------------------------------------

def proximo_dia_util(a_partir_de: datetime, dias_uteis: int = 1) -> datetime:
    """Retorna o dia útil `dias_uteis` à frente de `a_partir_de`, às 16:00.
    dias_uteis=1 -> próximo dia útil (regra de negócio real).
    dias_uteis=2 -> o dia útil seguinte a esse, e assim por diante.
    TODO: incluir feriados nacionais/municipais se necessário (ex.: lib `workalendar`).
    """
    d = a_partir_de
    contados = 0
    while contados < dias_uteis:
        d += timedelta(days=1)
        if d.weekday() < 5:  # 0-4 = seg-sex
            contados += 1
    return d.replace(hour=16, minute=0, second=0, microsecond=0)


# ------------------------------------------------------------------
# NAVEGAÇÃO / SCRAPING
# ------------------------------------------------------------------

def debug_screenshot(driver, nome):
    """Salva um print da tela atual + um trecho do HTML visível, para
    diagnosticar em que estado o navegador ficou quando algo falha.
    Como o script roda numa instância do Chrome separada (aberta pelo
    Selenium, não a que você usa manualmente), não há como eu ver essa tela
    ao vivo — rode e me envie o arquivo .png gerado quando algo travar.
    """
    caminho = Path(__file__).parent / f"debug_{nome}.png"
    try:
        driver.save_screenshot(str(caminho))
        print(f"[DEBUG] Screenshot salvo em: {caminho}")
    except Exception as e:
        print(f"[DEBUG] Não consegui salvar screenshot: {e}")
    print(f"[DEBUG] URL atual: {driver.current_url}")


def login(driver):
    """TODO: mapear a tela de login real do eOrder (usuário/senha, possível SSO/2FA).
    Este é um placeholder - ajuste os seletores após inspecionar a tela de login.

    IMPORTANTE: `webdriver.Chrome()` abre uma instância NOVA e separada do
    Chrome (perfil limpo, sem os cookies/sessão do navegador que você usa
    manualmente). Por isso o login precisa acontecer de verdade aqui dentro
    — não é possível reaproveitar a aba que você já deixou logada.
    """
    driver.get(BASE_URL)
    wait = WebDriverWait(driver, WAIT_TIMEOUT)

    # Exemplo genérico - AJUSTAR conforme a tela real:
    user_field = wait.until(EC.presence_of_element_located((By.NAME, "USER")))
    user_field.send_keys(EORDER_USER)
    pass_field = driver.find_element(By.ID, "INPUTPASS")
    pass_field.send_keys(EORDER_PASS)
    pass_field.send_keys(Keys.RETURN)
    time.sleep(10)
    # Confirma que o login realmente terminou (a app é GWT e pode demorar
    # para renderizar após o POST de login) antes de seguir navegando.
    # Timeout longo porque o primeiro carregamento (sem cache) costuma ser
    # bem mais lento do que abrir a mesma página numa aba já usada antes.
    try:
        WebDriverWait(driver, 45).until(
            EC.presence_of_element_located(
                (By.XPATH, "//*[contains(normalize-space(text()),'Menu Principal')]")
            )
        )
    except Exception:
        debug_screenshot(driver, "login_falhou")
        raise RuntimeError(
            "Login não confirmado: não encontrei 'Menu Principal' após enviar "
            "usuário/senha. Veja o screenshot debug_login_falhou.png — pode ser "
            "senha incorreta, MFA/aviso de segurança, ou apenas carregamento "
            "mais lento que o esperado."
        )




def esperar_e_clicar_visivel(driver, xpath, timeout=45):
    """Espera até existir um elemento que bata com o XPath E esteja
    realmente visível/habilitado na tela, então clica nele.

    Por que não usar WebDriverWait + element_to_be_clickable direto: essa
    condição localiza só o PRIMEIRO elemento que bate com o XPath e espera
    só ele ficar clicável. Nessa aplicação (GWT), formulários de outras
    abas continuam no DOM mesmo escondidos (não são removidos) — se um
    desses elementos ocultos aparecer antes do visível na ordem do
    documento, o Selenium fica esperando para sempre por um elemento que
    nunca vai ficar visível, mesmo com o elemento certo já na tela.

    Esta função busca TODOS os elementos que batem com o XPath e usa o
    primeiro que estiver de fato visível e habilitado.
    """
    fim = time.time() + timeout
    ultimo_erro = None
    while time.time() < fim:
        try:
            candidatos = driver.find_elements(By.XPATH, xpath)
            for el in candidatos:
                try:
                    if el.is_displayed() and el.is_enabled():
                        try:
                            el.click()
                        except Exception:
                            # Fallback: clique via JS (contorna overlay/interceptação de clique)
                            driver.execute_script("arguments[0].click();", el)
                        return el
                except Exception as e:  # elemento pode ter ficado "stale" entre o find e o click
                    ultimo_erro = e
        except Exception as e:
            ultimo_erro = e
        time.sleep(0.4)
    raise TimeoutException(
        f"Nenhum elemento visível/clicável encontrado para XPath: {xpath!r} "
        f"em {timeout}s. Último erro interno: {ultimo_erro}"
    )


def encontrar_elemento_visivel(driver, by, valor, timeout=WAIT_TIMEOUT):
    """Mesmo motivo de esperar_e_clicar_visivel, mas para localizar (sem
    clicar) inputs/selects por `By.NAME` — essa app GWT também duplica
    campos de formulário de outras abas escondidos no DOM. Usar
    presence_of_element_located/find_element puro pega o primeiro elemento
    na ordem do DOM, que às vezes é o duplicado oculto (não o campo
    realmente visível na tela) — isso é o que causava o preenchimento da
    busca falhar/travar de forma intermitente. Aqui varremos todos os
    matches e retornamos o primeiro visível e habilitado.
    """
    fim = time.time() + timeout
    ultimo_erro = None
    while time.time() < fim:
        try:
            candidatos = driver.find_elements(by, valor)
            for el in candidatos:
                try:
                    if el.is_displayed() and el.is_enabled():
                        return el
                except Exception as e:
                    ultimo_erro = e
        except Exception as e:
            ultimo_erro = e
        time.sleep(0.3)
    raise TimeoutException(
        f"Nenhum elemento visível encontrado para {by}={valor!r} em {timeout}s. "
        f"Último erro interno: {ultimo_erro}"
    )


def click_by_text(driver, text, tag="*", timeout=WAIT_TIMEOUT):
    """Localiza por texto visível (inclusive dentro de spans/ícones
    aninhados, via string-value com '.') e clica, ignorando matches ocultos.
    """
    xpath = f"//{tag}[not(*) and contains(normalize-space(.),'{text}')]"
    return esperar_e_clicar_visivel(driver, xpath, timeout=timeout)


def navegar_ate_atividades_programaveis(driver):
    # 1. Menu lateral: Planejamento
    click_by_text(driver, "Planejamento", tag="div")
    time.sleep(0.5)

    # 2. Submenu: Plano Diário
    click_by_text(driver, "Plano Diário", tag="div")
    time.sleep(1)

    # 3. O painel geral ("gerais", botão Busca) abre sozinho ao entrar em
    #    Plano Diário. É preciso clicar em "Busca" (submit) para carregar os
    #    dados iniciais — SÓ DEPOIS que essa busca termina de carregar (pode
    #    levar vários segundos) é que a faixa de abas superiores aparece
    #    ("Plano Diário | Clipboard | Atividades Programáveis | ...").
    #    NÃO é necessário fechar o painel manualmente — ele recolhe sozinho
    #    quando o resultado termina de carregar.
    #
    #    Usamos esperar_e_clicar_visivel (em vez de element_to_be_clickable)
    #    porque essa app mantém formulários de OUTRAS abas escondidos no DOM
    #    em vez de removê-los — pode haver mais de um botão "Busca"/aba com
    #    o mesmo texto, e o Selenium travaria esperando um elemento oculto
    #    (de outra aba) virar visível, mesmo com o botão certo já na tela.
    try:
        esperar_e_clicar_visivel(
            driver,
            "//button[contains(@class,'butSub') and contains(normalize-space(.),'Busca')]",
            timeout=45,
        )
    except Exception:
        debug_screenshot(driver, "botao_busca_nao_encontrado")
        raise

    # 4. Aguarda até a aba "Atividades Programáveis" ficar visível e clica.
    #    O screenshot de debug mostrou o botão "Busca" ainda GIRANDO (carregando)
    #    depois de 45s — essa busca geral carrega ~3000 registros (Cortes,
    #    Reconexões) e pode legitimamente demorar mais que isso dependendo do
    #    servidor. Aumentado para 120s.
    try:
        esperar_e_clicar_visivel(
            driver,
            "//div[not(*) and contains(normalize-space(.),'Atividades Programáveis')]",
            timeout=120,
        )
    except Exception:
        debug_screenshot(driver, "aba_atividades_programaveis_nao_encontrada")
        raise
    time.sleep(1)


def listar_elementos_candidatos_fechar(driver):
    """Ferramenta de diagnóstico: lista os elementos da coluna de ícones do
    painel lateral direito (tag, classe, atributos) para identificar o
    seletor correto do ícone de fechar."""
    script = """
    const painel = Array.from(document.querySelectorAll('*'))
        .filter(e => e.children.length === 0 && e.offsetParent !== null)
        .filter(e => e.tagName === 'IMG' || e.tagName === 'DIV' || e.tagName === 'BUTTON');
    return painel.slice(0, 60).map(e => ({
        tag: e.tagName, cls: e.className, id: e.id,
        src: e.src || null, title: e.title || null,
        text: (e.textContent||'').trim().slice(0,20)
    }));
    """
    resultado = driver.execute_script(script)
    for item in resultado:
        print(item)
    return resultado


def preencher_busca(driver, prazo_final: datetime):
    # Selecionar busca salva. Usa encontrar_elemento_visivel (não
    # presence_of_element_located puro) porque essa app GWT mantém campos de
    # OUTRAS abas escondidos no DOM com o mesmo `name` — pegar o primeiro
    # elemento por ordem do DOM às vezes acerta o duplicado oculto, o que
    # fazia a seleção da busca falhar de forma intermitente.
    select_el = encontrar_elemento_visivel(driver, By.NAME, "_lyXWFMARSAID", timeout=WAIT_TIMEOUT)
    Select(select_el).select_by_value(SAVED_SEARCH_VALUE)

    # Expandir seção "Datas" caso esteja colapsada (clicar no cabeçalho "Datas")
    try:
        click_by_text(driver, "Datas", tag="div", timeout=3)
        time.sleep(0.3)
    except Exception:
        pass  # já pode estar expandida

    # Campo de data "Prazo ANS Legal - A"
    data_field = encontrar_elemento_visivel(driver, By.NAME, "_dyDATASCADANSLEG_A", timeout=WAIT_TIMEOUT)
    data_field.clear()
    data_field.send_keys(prazo_final.strftime("%d/%m/%Y"))

    # Campo de horário "Prazo ANS Legal - A"
    hora_field = encontrar_elemento_visivel(driver, By.NAME, "_dyORASCADANSLEG_A", timeout=WAIT_TIMEOUT)
    hora_field.clear()
    hora_field.send_keys(prazo_final.strftime("%H:%M"))

    # Como esta função é chamada repetidamente pelo loop (main), o título
    # "Lista Atividades (N)" da checagem ANTERIOR já pode estar na tela.
    # Guarda essa referência antes de clicar em "Busca" de novo, para depois
    # esperar ela ficar "stale" (o GWT recria o elemento) — sem isso, o
    # wait.until(presence_of_element_located(...)) logo abaixo passaria na
    # hora, pegando o resultado ANTIGO em vez de esperar o novo carregar.
    titulo_antigo = None
    try:
        titulo_antigo = driver.find_element(By.XPATH, "//*[contains(text(),'Lista Atividades')]")
    except Exception:
        titulo_antigo = None

    # Botão Busca (mesmo cuidado do passo anterior: pode haver mais de um
    # botão "Busca" no DOM, oculto em outra aba/painel)
    try:
        esperar_e_clicar_visivel(
            driver,
            "//button[contains(@class,'butSub') and contains(normalize-space(.),'Busca')]",
            timeout=45,
        )
    except Exception:
        debug_screenshot(driver, "botao_busca_atividades_programaveis_nao_encontrado")
        raise

    if titulo_antigo is not None:
        try:
            WebDriverWait(driver, 45).until(EC.staleness_of(titulo_antigo))
        except Exception:
            pass  # se não detectar a troca a tempo, segue e tenta achar o novo mesmo assim

    # Aguardar resultados carregarem (título "Lista Atividades (N)")
    try:
        WebDriverWait(driver, 45).until(
            EC.presence_of_element_located((By.XPATH, "//*[contains(text(),'Lista Atividades')]"))
        )
    except Exception:
        debug_screenshot(driver, "lista_atividades_nao_carregou")
        raise
    time.sleep(1)


def extrair_registros(driver, tentativas=5):
    """Localiza a tabela de resultados (table.tvGrid cujo cabeçalho contém
    'Código TdC') e extrai os registros como lista de dicts.

    O GWT continua re-renderizando a tabela por um tempo depois que "Lista
    Atividades" aparece na tela, o que invalida (stale) os elementos já
    capturados. Por isso a extração inteira roda dentro de um retry: se
    qualquer parte esbarrar num StaleElementReferenceException, esperamos
    um pouco e buscamos os elementos do zero.
    """
    ultimo_erro = None
    for tentativa in range(tentativas):
        try:
            tabelas = driver.find_elements(By.CSS_SELECTOR, "table.tvGrid")
            tabela_resultados = None
            for t in tabelas:
                trs = t.find_elements(By.TAG_NAME, "tr")
                header_text = trs[0].text if trs else ""
                if "Código TdC" in header_text:
                    tabela_resultados = t
                    break

            if tabela_resultados is None:
                raise RuntimeError("Tabela de resultados não encontrada — verifique se a busca retornou dados.")

            linhas = tabela_resultados.find_elements(By.TAG_NAME, "tr")[1:]  # pula cabeçalho
            registros = []
            for linha in linhas:
                # A grade sempre renderiza um número fixo de linhas (padding),
                # mesmo sem dados: linhas vazias vêm com class "tvRowEmpty" e
                # células só com "&nbsp;". Pular essas ANTES de montar o
                # registro (também há uma checagem de segurança abaixo, caso
                # o padrão de classe mude).
                linha_classe = linha.get_attribute("class") or ""
                if "tvRowEmpty" in linha_classe:
                    continue

                celulas = [c.text.strip() for c in linha.find_elements(By.TAG_NAME, "td")]
                if len(celulas) < 12:
                    continue

                # Segurança extra: se a "chave" (Código TdC) vier vazia,
                # trata como linha de padding e ignora, independente da classe.
                if not celulas[1]:
                    continue

                registro = {
                    "codigo_tdc": celulas[1],
                    "numero_servico": celulas[2],
                    "codigo_cliente_medidor": celulas[3],
                    "intervalo_execucao": celulas[4],
                    "endereco": celulas[5],
                    "tipo_servico": celulas[6],
                    "tipo_remessa_win": celulas[7],
                    "des_atividade": celulas[8],
                    "configuracao_skill": celulas[10],
                    "ans_legal_countdown": celulas[11],
                }
                # Extrai a data/hora de vencimento (fim do intervalo, após "a ")
                try:
                    partes = registro["intervalo_execucao"].split("\na ")
                    registro["vencimento"] = partes[1].strip() if len(partes) > 1 else None
                except Exception:
                    registro["vencimento"] = None
                registros.append(registro)

            return registros

        except StaleElementReferenceException as e:
            ultimo_erro = e
            time.sleep(1)
            continue

    debug_screenshot(driver, "extracao_stale_apos_tentativas")
    raise RuntimeError(
        f"Não consegui extrair os registros após {tentativas} tentativas "
        f"(elementos ficaram stale repetidamente). Último erro: {ultimo_erro}"
    )


def obter_total_declarado(driver):
    """Lê o total real de registros a partir do título 'Lista Atividades (N)'.

    Importante: a grade mostra no máximo ~25 linhas por página (o resto vai
    para outra página, com paginação) — então NÃO dá pra confiar em
    len(registros) da extração acima como "quantidade total" se houver mais
    de uma página. Esse título traz o total oficial.
    TODO: se precisar do detalhe (endereço, TdC etc.) de TODOS os registros
    quando houver mais de uma página, será necessário implementar navegação
    de paginação aqui — por ora extrai apenas os registros da página atual.
    """
    try:
        el = driver.find_element(By.XPATH, "//*[contains(text(),'Lista Atividades')]")
        m = re.search(r"Lista Atividades\s*\((\d+)\)", el.text)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


def vencimento_mais_proximo(registros):
    """Retorna o datetime de vencimento mais próximo entre os registros
    extraídos (campo 'vencimento', formato 'DD/MM/AAAA HH:MM')."""
    datas = []
    for r in registros:
        v = r.get("vencimento")
        if not v:
            continue
        try:
            datas.append(datetime.strptime(v, "%d/%m/%Y %H:%M"))
        except Exception:
            continue
    return min(datas) if datas else None


def montar_mensagem(quantidade, vencimento, novos_count=0, permanecem_count=0, primeira_verificacao=True):
    """Monta o texto de status (usado tanto no console quanto na
    notificação push). Regras:

    - quantidade == 0: mensagem simples ("Há 0 religações"); o chamador
      decide não disparar push nesse caso.
    - primeira_verificacao=True (não há snapshot anterior salvo, ou é a
      primeira vez que aparece alguma religação): reporta o total, sem
      falar em "novas" — não há uma verificação anterior para comparar.
    - Caso contrário: se entraram religações novas desde a última checagem,
      destaca quantas são novas e quantas já estavam presentes; senão,
      informa que não houve novidade.

    Em qualquer caso com quantidade > 0, inclui o vencimento mais próximo
    considerando TODAS as religações atualmente na página (novas + as que
    já estavam lá).
    """
    if not quantidade:
        return "Há 0 religações em sistema."

    venc_txt = (
        f" Vencimento mais próximo: {vencimento.strftime('%d/%m/%Y %H:%M')}."
        if vencimento else ""
    )

    if primeira_verificacao:
        return f"Há {quantidade} religações em sistema.{venc_txt}"

    if novos_count > 0:
        texto = f"Entraram {novos_count} nova(s) religação(ões) no sistema"
        if permanecem_count > 0:
            texto += f" (as {permanecem_count} anteriores continuam)"
        texto += f". Total: {quantidade} religações em sistema.{venc_txt}"
        return texto

    return (
        f"Continuam {quantidade} religações em sistema "
        f"(nenhuma nova desde a última verificação).{venc_txt}"
    )


# ------------------------------------------------------------------
# COMPARAÇÃO / ESTADO (snapshot em disco — usado para saber quais códigos
# TdC já eram conhecidos na verificação anterior, e assim distinguir
# religações novas das que já estavam presentes)
# ------------------------------------------------------------------

def carregar_estado_anterior():
    """Lê o snapshot salvo na verificação ANTERIOR (antes de ser
    sobrescrito nesta checagem). Retorna None se ainda não existe nenhum
    estado salvo (primeira execução) ou se o arquivo estiver corrompido."""
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def calcular_diferenca(registros, estado_anterior):
    """Compara os códigos TdC extraídos agora com os que já estavam no
    snapshot da verificação anterior. Retorna (ids_novos, ids_permanecem)."""
    ids_atuais = {r["codigo_tdc"] for r in registros}
    ids_anteriores = set(estado_anterior["registros"].keys()) if estado_anterior else set()
    novos = ids_atuais - ids_anteriores
    permanecem = ids_atuais & ids_anteriores
    return novos, permanecem


def salvar_estado(registros, quantidade_declarada):
    """Snapshot da última checagem — usado por carregar_estado_anterior()
    no próximo ciclo para detectar quais religações são novas."""
    estado = {
        "quantidade_declarada": quantidade_declarada,
        "atualizado_em": datetime.now().isoformat(),
        "registros": {r["codigo_tdc"]: r for r in registros},
    }
    STATE_FILE.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------------
# NOTIFICAÇÃO PUSH (ntfy.sh)
# ------------------------------------------------------------------

def enviar_notificacao_push(titulo, mensagem, prioridade=3, tags=None):
    """Envia uma notificação push via ntfy.sh — chega no celular e no
    desktop de qualquer pessoa inscrita em NTFY_TOPIC. Nunca derruba o loop
    principal por falha de rede/serviço: só registra o erro no console.

    prioridade: 1 (mín.) a 5 (máx.) — ver https://docs.ntfy.sh/publish/#message-priority
    tags: lista de "emoji shortcodes" do ntfy (ex.: ["rotating_light"]) —
    viram ícone na notificação. Ver https://docs.ntfy.sh/publish/#tags-emojis
    """
    if not HABILITAR_NOTIFICACAO_PUSH:
        return
    payload = {
        "topic": NTFY_TOPIC,
        "title": titulo,
        "message": mensagem,
        "priority": prioridade,
        "tags": tags or [],
    }
    try:
        resp = requests.post(NTFY_SERVER, json=payload, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"[NOTIFICAÇÃO] Falha ao enviar push via ntfy: {e}")


# ------------------------------------------------------------------
# SERVIDOR HTTP DE HEALTH-CHECK
# ------------------------------------------------------------------
# Existe só para o bot poder ser hospedado como "Web Service" no plano free
# do Render (que exige um processo ouvindo em $PORT — background worker e
# cron job não têm plano gratuito lá). Também serve de alvo para o ping
# externo que mantém a instância acordada durante a janela permitida (ver
# comentário em JANELA DE FUNCIONAMENTO acima).

_status_lock = threading.Lock()
_status = {"estado": "iniciando", "atualizado_em": None}


def _atualizar_status(estado: str):
    with _status_lock:
        _status["estado"] = estado
        _status["atualizado_em"] = datetime.now(TIMEZONE).isoformat()


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        with _status_lock:
            corpo = json.dumps(_status).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def log_message(self, format, *args):
        pass  # silencia o log padrão de cada request (o ping bate a cada poucos minutos)


def iniciar_servidor_saude():
    porta = int(os.environ.get("PORT", 8080))
    servidor = ThreadingHTTPServer(("0.0.0.0", porta), _HealthHandler)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    print(f"[HTTP] Servidor de health-check ouvindo na porta {porta}")


# ------------------------------------------------------------------
# MAIN — roda em loop, verificando a cada INTERVALO_VERIFICACAO_SEGUNDOS
# ------------------------------------------------------------------

def verificar_uma_vez(driver):
    """Roda um ciclo completo de checagem: busca, extrai, compara com o
    snapshot da verificação anterior (para saber o que é novo), envia
    notificação push apenas se alguma religação NOVA entrou desde a última
    checagem, e retorna o texto de status para o console (o mesmo texto
    enviado no push, quando enviado)."""
    prazo = proximo_dia_util(datetime.now(), dias_uteis=DIAS_UTEIS_PRAZO)
    preencher_busca(driver, prazo)

    registros = extrair_registros(driver)
    quantidade = obter_total_declarado(driver)
    if quantidade is None:
        # Título "Lista Atividades (N)" não encontrado por algum motivo;
        # usa a contagem de registros extraídos como alternativa.
        quantidade = len(registros)

    # Lê o estado da checagem ANTERIOR antes de sobrescrevê-lo, para poder
    # comparar e descobrir quais códigos TdC são novos nesta rodada.
    estado_anterior = carregar_estado_anterior()
    ids_novos, ids_permanecem = calcular_diferenca(registros, estado_anterior)

    vencimento = vencimento_mais_proximo(registros)
    texto = montar_mensagem(
        quantidade,
        vencimento,
        novos_count=len(ids_novos),
        permanecem_count=len(ids_permanecem),
        primeira_verificacao=(estado_anterior is None),
    )

    salvar_estado(registros, quantidade)

    # Só notifica via push quando há religação NOVA desde a checagem
    # anterior — evita ficar reavisando, a cada ciclo, das mesmas N
    # religações que já estavam no sistema e não mudaram.
    if ids_novos:
        enviar_notificacao_push(
            titulo="CENEGED Religação Maricá",
            mensagem=texto,
            prioridade=4,
            tags=["rotating_light"],
        )

    return texto


def _abrir_driver():
    # Flags de baixo consumo de memória — necessárias porque o plano free do
    # Render dá só 512MB de RAM, e o Chromium headless "padrão" facilmente
    # ultrapassa isso (o primeiro deploy morreu com "Ran out of memory (used
    # over 512MB)" só de abrir o navegador). Não há garantia de que isso seja
    # suficiente contra a grade pesada do eOrder (GWT, ~3000 registros na
    # busca geral) — se continuar estourando memória, o próximo passo é subir
    # de plano (Standard, 2GB RAM), não tem mais o que cortar no Chromium.
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=old")  # headless "antigo" pesa menos que o novo (=new)
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--single-process")  # funde processo browser+renderer (economiza bastante RAM)
    options.add_argument("--no-zygote")
    options.add_argument("--window-size=1024,768")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-breakpad")
    options.add_argument("--disable-component-extensions-with-background-pages")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-translate")
    options.add_argument("--metrics-recording-only")
    options.add_argument("--mute-audio")
    options.add_argument("--no-first-run")
    options.add_argument("--disk-cache-size=1")
    options.add_argument("--renderer-process-limit=1")
    options.add_argument("--js-flags=--max-old-space-size=192")

    # No Docker (Render) o Chromium/driver do sistema são apontados por
    # essas variáveis (ver Dockerfile); localmente, sem elas, o Selenium usa
    # o Chrome padrão da máquina.
    chrome_bin = os.environ.get("CHROME_BIN")
    if chrome_bin:
        options.binary_location = chrome_bin

    chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")
    if chromedriver_path:
        return webdriver.Chrome(options=options, service=ChromeService(executable_path=chromedriver_path))
    return webdriver.Chrome(options=options)


def executar_ciclo_bot():
    """Loop principal: só liga o navegador e faz login/checagens quando
    `dentro_da_janela_permitida()` é True. Fora da janela, garante que o
    driver fique fechado (economiza RAM/CPU) e só reconfere o relógio a
    cada FORA_DA_JANELA_INTERVALO_SEGUNDOS."""
    driver = None
    falhas_consecutivas = 0

    try:
        while True:
            agora = datetime.now(TIMEZONE)
            agora_str = agora.strftime("%d/%m/%Y %H:%M:%S")

            if not dentro_da_janela_permitida(agora):
                if driver is not None:
                    print(f"[{agora_str}] Fora do horário permitido (seg-sex {HORARIO_INICIO}h-{HORARIO_FIM}h) — encerrando navegador.")
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    driver = None
                    falhas_consecutivas = 0
                _atualizar_status("fora_do_horario")
                time.sleep(FORA_DA_JANELA_INTERVALO_SEGUNDOS)
                continue

            if driver is None:
                print(f"[{agora_str}] Dentro do horário permitido — iniciando navegador e login.")
                _atualizar_status("iniciando_sessao")
                try:
                    driver = _abrir_driver()
                    login(driver)  # TODO: implementar login real
                    navegar_ate_atividades_programaveis(driver)
                except Exception as e:
                    print(f"[{agora_str}] Falha ao iniciar sessão: {e}")
                    if driver is not None:
                        try:
                            driver.quit()
                        except Exception:
                            pass
                        driver = None
                    _atualizar_status(f"erro_login: {e}")
                    time.sleep(INTERVALO_VERIFICACAO_SEGUNDOS)
                    continue

            try:
                texto = verificar_uma_vez(driver)
                falhas_consecutivas = 0
                print(f"[{agora_str}] {texto}")
                _atualizar_status("ok")
            except Exception as e:
                # Não derruba o loop por causa de uma falha pontual (ex.:
                # instabilidade momentânea da página) — registra e tenta de
                # novo no próximo ciclo. Mas se o erro se repetir várias
                # vezes seguidas, a página provavelmente ficou num estado
                # quebrado (sessão caiu, navegação perdida etc.) e insistir
                # do mesmo jeito só fica "travado" para sempre — por isso
                # tentamos recuperar ativamente em vez de só logar.
                falhas_consecutivas += 1
                print(f"[{agora_str}] ERRO na verificação (falha {falhas_consecutivas} seguida(s)): {e}")
                debug_screenshot(driver, "erro_ciclo_verificacao")
                _atualizar_status(f"erro: {e}")

                if falhas_consecutivas >= MAX_FALHAS_CONSECUTIVAS_ANTES_DE_RECUPERAR:
                    print(f"[{agora_str}] {falhas_consecutivas} falhas seguidas — tentando recuperar renavegando.")
                    try:
                        navegar_ate_atividades_programaveis(driver)
                        falhas_consecutivas = 0
                    except Exception as e_nav:
                        print(f"[{agora_str}] Renavegação falhou ({e_nav}); tentando relogar do zero.")
                        debug_screenshot(driver, "falha_recuperacao_navegacao")
                        try:
                            login(driver)
                            navegar_ate_atividades_programaveis(driver)
                            falhas_consecutivas = 0
                        except Exception as e_login:
                            print(f"[{agora_str}] Relogin também falhou ({e_login}); descartando sessão para tentar de novo no próximo ciclo.")
                            debug_screenshot(driver, "falha_recuperacao_relogin")
                            try:
                                driver.quit()
                            except Exception:
                                pass
                            driver = None

            time.sleep(INTERVALO_VERIFICACAO_SEGUNDOS)

    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def main():
    iniciar_servidor_saude()
    try:
        executar_ciclo_bot()
    except KeyboardInterrupt:
        print("Encerrado pelo usuário.")


if __name__ == "__main__":
    main()
