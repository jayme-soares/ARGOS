import os
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService

from argos import config


def abrir_driver(pasta_download: Path | None = None):
    """Abre um Chrome novo (perfil limpo). Com `pasta_download`, os
    downloads vão direto para essa pasta, sem perguntar — necessário para a
    exportação da Busca TdC."""
    options = webdriver.ChromeOptions()
    if config.CHROME_HEADLESS:
        options.add_argument("--headless=old")  # headless "antigo" pesa menos que o novo (=new)
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    # NÃO usar --single-process: economiza RAM mas o Chromium fica instável e
    # pode travar (deadlock) sem lançar exceção nenhuma — aconteceu em
    # produção. O comando com timeout abaixo é a segunda camada de proteção.
    options.add_argument("--window-size=1366,900")
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
    if config.CHROME_BAIXA_MEMORIA:
        # Herdadas do plano free do Render (512MB), onde o Chromium padrão
        # estourava a memória só de abrir.
        options.add_argument("--disk-cache-size=1")
        options.add_argument("--renderer-process-limit=1")
        options.add_argument("--js-flags=--max-old-space-size=192")

    if pasta_download is not None:
        pasta_download.mkdir(parents=True, exist_ok=True)
        options.add_experimental_option("prefs", {
            "download.default_directory": str(pasta_download.resolve()),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True,
        })

    # No Docker o Chromium/driver do sistema são apontados por essas
    # variáveis (ver Dockerfile); localmente, sem elas, o Selenium usa o
    # Chrome padrão da máquina.
    chrome_bin = os.environ.get("CHROME_BIN")
    if chrome_bin:
        options.binary_location = chrome_bin

    chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")
    if chromedriver_path:
        driver = webdriver.Chrome(options=options, service=ChromeService(executable_path=chromedriver_path))
    else:
        driver = webdriver.Chrome(options=options)

    # Sem isso, um comando Selenium que trave esperando o chromedriver (ex.:
    # Chromium engasgado sob CPU throttling) bloqueia para sempre — nenhuma
    # exceção é levantada e o loop simplesmente para. Com o timeout, o
    # comando falha e vira uma exceção normal, que cai na recuperação.
    driver.command_executor.set_timeout(config.COMANDO_TIMEOUT_SEGUNDOS)

    if pasta_download is not None:
        # Em headless o Chrome ignora a pref de download sem esse comando CDP.
        caminho = str(pasta_download.resolve())
        try:
            driver.execute_cdp_cmd("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": caminho})
        except Exception:
            driver.execute_cdp_cmd("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": caminho})

    return driver


def fechar_driver(driver):
    if driver is None:
        return
    try:
        driver.quit()
    except Exception:
        pass
