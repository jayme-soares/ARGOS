import time

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from argos import config
from argos.eorder.ui import debug_screenshot


def login(driver):
    """Loga no eOrder. `webdriver.Chrome()` abre uma instância nova (perfil
    limpo, sem cookies), então o login sempre acontece de verdade aqui."""
    driver.get(config.BASE_URL)
    wait = WebDriverWait(driver, config.WAIT_TIMEOUT)

    user_field = wait.until(EC.presence_of_element_located((By.NAME, "USER")))
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
