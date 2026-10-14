import time

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from argos import config
from argos.eorder.ui import debug_screenshot


def login(driver):
    """Loga no eOrder. `webdriver.Chrome()` abre uma instância nova (perfil
    limpo, sem cookies), então o primeiro login acontece de verdade aqui.

    Também serve para recomeçar do Menu Principal no meio de uma sessão
    (troca de CO, tela quebrada): com a sessão ainda viva o eOrder não mostra
    o formulário, vai direto ao Menu Principal. Por isso apaga os cookies
    antes e, se mesmo assim cair no menu, aceita."""
    try:
        driver.delete_all_cookies()
    except Exception:
        pass
    driver.get(config.BASE_URL)
    wait = WebDriverWait(driver, config.WAIT_TIMEOUT)

    wait.until(
        EC.any_of(
            EC.presence_of_element_located((By.NAME, "USER")),
            EC.presence_of_element_located((By.XPATH, "//*[contains(normalize-space(text()),'Menu Principal')]")),
        )
    )
    campos = driver.find_elements(By.NAME, "USER")
    if not campos:
        return
    user_field = campos[0]
    user_field.clear()
    user_field.send_keys(config.EORDER_USER)
    pass_field = driver.find_element(By.ID, "INPUTPASS")
    pass_field.clear()
    pass_field.send_keys(config.EORDER_PASS)
    pass_field.send_keys(Keys.RETURN)
    time.sleep(10)
    # Confirma que o login realmente terminou (a app é GWT e pode demorar
    # para renderizar após o POST) antes de seguir navegando. Timeout longo
    # porque o primeiro carregamento (sem cache) costuma ser bem mais lento.
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
            "usuário/senha. Veja o screenshot debug_login_falhou — pode ser "
            "senha incorreta, MFA/aviso de segurança, ou apenas carregamento "
            "mais lento que o esperado."
        )
