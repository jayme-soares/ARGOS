"""Helpers de interação com a interface GWT do eOrder."""

import time
import unicodedata
from datetime import datetime

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select

from argos import config
from argos.log import log

# Botão "Busca" dos painéis de pesquisa. O eOrder trocou a classe do botão
# (era "butSub", passou a ser "but butAct butRO"), o que quebrou o seletor
# antigo. Agora casa pela ação disparada no onclick ('QuerySoloInt#'), que é
# independente de estilo; o texto "Busca" fica como alternativa.
XPATH_BOTAO_BUSCA = (
    "//button[contains(@onclick,'QuerySoloInt#')"
    " or (contains(@class,'but') and contains(normalize-space(.),'Busca'))]"
)


def debug_screenshot(driver, nome):
    """Salva um print da tela atual em DATA_DIR/debug para diagnosticar em
    que estado o navegador ficou quando algo falha (o Chrome roda headless,
    não há como ver a tela ao vivo)."""
    config.DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    carimbo = datetime.now(config.TIMEZONE).strftime("%Y%m%d_%H%M%S")
    caminho = config.DEBUG_DIR / f"debug_{nome}_{carimbo}.png"
    try:
        driver.save_screenshot(str(caminho))
        log(f"Screenshot salvo em: {caminho}", "DEBUG")
    except Exception as e:
        log(f"Não consegui salvar screenshot: {e}", "DEBUG")
    try:
        log(f"URL atual: {driver.current_url}", "DEBUG")
    except Exception:
        pass
    _limpar_screenshots_antigos()


def _limpar_screenshots_antigos(manter=40):
    # Com o bot rodando sem parar, os prints de erro acumulariam para sempre
    # no volume.
    try:
        arquivos = sorted(config.DEBUG_DIR.glob("debug_*.png"), key=lambda p: p.stat().st_mtime)
        for arq in arquivos[:-manter]:
            arq.unlink(missing_ok=True)
    except Exception:
        pass


def esperar_e_clicar_visivel(driver, xpath, timeout=45, via_js=False):
    """Espera até existir um elemento que bata com o XPath E esteja
    realmente visível/habilitado na tela, então clica nele.

    Por que não usar WebDriverWait + element_to_be_clickable direto: essa
    condição localiza só o PRIMEIRO elemento que bate com o XPath. Nessa
    aplicação (GWT), formulários de outras abas continuam no DOM mesmo
    escondidos — se um desses elementos ocultos aparecer antes do visível na
    ordem do documento, o Selenium fica esperando para sempre por um elemento
    que nunca vai ficar visível. Aqui usamos o primeiro que estiver visível.
    """
    fim = time.time() + timeout
    ultimo_erro = None
    while time.time() < fim:
        try:
            for el in driver.find_elements(By.XPATH, xpath):
                try:
                    if el.is_displayed() and el.is_enabled():
                        if via_js:
                            driver.execute_script("arguments[0].click();", el)
                        else:
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


def clicar_primeiro_disponivel(driver, xpaths, timeout=45, via_js=False):
    """Tenta uma lista de XPaths alternativos (do mais robusto ao mais
    frágil) dividindo o timeout entre eles, em rodadas."""
    fim = time.time() + timeout
    ultimo_erro = None
    while time.time() < fim:
        for xp in xpaths:
            try:
                return esperar_e_clicar_visivel(driver, xp, timeout=2, via_js=via_js)
            except TimeoutException as e:
                ultimo_erro = e
    raise TimeoutException(f"Nenhuma das alternativas ficou clicável em {timeout}s: {xpaths!r}. {ultimo_erro}")


def encontrar_elemento_visivel(driver, by, valor, timeout=config.WAIT_TIMEOUT):
    """Mesmo motivo de esperar_e_clicar_visivel, mas para localizar (sem
    clicar) inputs/selects — essa app GWT também duplica campos de
    formulário de outras abas escondidos no DOM. Retorna o primeiro match
    visível e habilitado.
    """
    fim = time.time() + timeout
    ultimo_erro = None
    while time.time() < fim:
        try:
            for el in driver.find_elements(by, valor):
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


def click_by_text(driver, text, tag="*", timeout=config.WAIT_TIMEOUT):
    """Localiza por texto visível (inclusive dentro de spans/ícones
    aninhados, via string-value com '.') e clica, ignorando matches ocultos.
    """
    xpath = f"//{tag}[not(*) and contains(normalize-space(.),'{text}')]"
    return esperar_e_clicar_visivel(driver, xpath, timeout=timeout)


def normalizar_texto(texto: str) -> str:
    """Sem acento, sem espaços extras, minúsculo — o eOrder às vezes grava
    nomes de busca sem acento ("MARICA") e com espaços de padding."""
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFKD", texto or "") if not unicodedata.combining(c)
    )
    return " ".join(sem_acento.split()).casefold()


def selecionar_opcao_por_texto(driver, texto, timeout=45):
    """Procura, entre todos os <select> visíveis, a <option> cujo texto
    normalizado bate com `texto` e a seleciona pelo value.

    Não usa Select.select_by_visible_text porque as opções do eOrder vêm com
    espaços de padding (e às vezes sem acento), e não depende de XPath
    absoluto, que quebra a cada mudança de layout.
    """
    alvo = normalizar_texto(texto)
    fim = time.time() + timeout
    ultimo_erro = None
    while time.time() < fim:
        try:
            for sel in driver.find_elements(By.TAG_NAME, "select"):
                try:
                    if not sel.is_displayed():
                        continue
                    for opt in sel.find_elements(By.TAG_NAME, "option"):
                        if normalizar_texto(opt.text) == alvo:
                            Select(sel).select_by_value(opt.get_attribute("value"))
                            return sel
                except Exception as e:
                    ultimo_erro = e
        except Exception as e:
            ultimo_erro = e
        time.sleep(0.5)
    raise TimeoutException(
        f"Opção {texto!r} não encontrada em nenhum <select> visível em {timeout}s. "
        f"Opções disponíveis: {_opcoes_visiveis(driver)}. Último erro interno: {ultimo_erro}"
    )


def _opcoes_visiveis(driver, limite=60):
    """Textos das <option> dos selects visíveis — para a mensagem de erro
    mostrar o nome exato cadastrado no eOrder."""
    textos = []
    try:
        for sel in driver.find_elements(By.TAG_NAME, "select"):
            if sel.is_displayed():
                textos += [" ".join(o.text.split()) for o in sel.find_elements(By.TAG_NAME, "option") if o.text.strip()]
    except Exception:
        pass
    return textos[:limite]
