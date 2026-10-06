"""Plano Diário > Atividades Programáveis > busca salva "CENEGED RELIGAÇÃO MARICA"."""

import re
import time
from datetime import datetime, timedelta

from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

from argos import config
from argos.eorder.ui import (
    XPATH_BOTAO_BUSCA,
    click_by_text,
    debug_screenshot,
    encontrar_elemento_visivel,
    esperar_e_clicar_visivel,
    normalizar_texto,
)


def proximo_dia_util(a_partir_de: datetime, dias_uteis: int = 1) -> datetime:
    """Retorna o dia útil `dias_uteis` à frente de `a_partir_de`, às 16:00.
    TODO: incluir feriados nacionais/municipais se necessário (ex.: lib `workalendar`).
    """
    d = a_partir_de
    contados = 0
    while contados < dias_uteis:
        d += timedelta(days=1)
        if d.weekday() < 5:  # 0-4 = seg-sex
            contados += 1
    return d.replace(hour=16, minute=0, second=0, microsecond=0)


def municipio_do_endereco(endereco: str | None) -> str:
    """Município no fim do endereço da grade: "RUA X 10 - BAIRRO, MARICA - RJ"
    -> "MARICA". A grade de Programáveis não tem coluna de município."""
    trecho = (endereco or "").rsplit(",", 1)[-1]
    return trecho.rsplit(" - ", 1)[0].strip()


def filtrar_municipio(registros: list[dict]) -> list[dict]:
    """A busca salva traz ordens de todo o Centro Operativo (Niterói também);
    só ficam as do município configurado (config.MUNICIPIO_CAMPO)."""
    alvo = normalizar_texto(config.MUNICIPIO_CAMPO)
    return [r for r in registros if normalizar_texto(municipio_do_endereco(r.get("endereco"))) == alvo]


def navegar_ate_atividades_programaveis(driver):
    # 1. Menu lateral: Planejamento > Plano Diário
    click_by_text(driver, "Planejamento", tag="div")
    time.sleep(0.5)
    click_by_text(driver, "Plano Diário", tag="div")
    time.sleep(1)

    # 2. O painel geral abre sozinho ao entrar em Plano Diário. É preciso
    #    escolher o Centro Operativo e clicar em "Busca" — SÓ DEPOIS que essa
    #    busca termina de carregar é que a faixa de abas superiores aparece
    #    ("Plano Diário | Clipboard | Atividades Programáveis | ...").
    try:
        centro_el = encontrar_elemento_visivel(driver, By.NAME, "_lyAUTEID_AFIL", timeout=config.WAIT_TIMEOUT)
        Select(centro_el).select_by_value(config.CENTRO_OPERATIVO_VALUE)
        # O GWT recarrega o painel após trocar o centro; clicar em "Busca"
        # antes disso faz o clique se perder (testado: 1s não bastava).
        time.sleep(3)
    except Exception:
        debug_screenshot(driver, "centro_operativo_nao_encontrado")
        raise

    try:
        esperar_e_clicar_visivel(driver, XPATH_BOTAO_BUSCA, timeout=45)
    except Exception:
        debug_screenshot(driver, "botao_busca_nao_encontrado")
        raise

    # 3. Aba "Atividades Programáveis". A busca geral carrega ~3000 registros
    #    (Cortes, Reconexões) e pode demorar bem mais que 45s.
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


def preencher_busca(driver, prazo_final: datetime):
    # Busca salva — encontrar_elemento_visivel porque o GWT mantém campos de
    # OUTRAS abas escondidos no DOM com o mesmo `name`.
    select_el = encontrar_elemento_visivel(driver, By.NAME, "_lyXWFMARSAID", timeout=config.WAIT_TIMEOUT)
    Select(select_el).select_by_value(config.SAVED_SEARCH_VALUE)

    # Expandir seção "Datas" caso esteja colapsada
    try:
        click_by_text(driver, "Datas", tag="div", timeout=3)
        time.sleep(0.3)
    except Exception:
        pass  # já pode estar expandida

    # "Prazo ANS Legal - A" (data e hora)
    data_field = encontrar_elemento_visivel(driver, By.NAME, "_dyDATASCADANSLEG_A", timeout=config.WAIT_TIMEOUT)
    data_field.clear()
    data_field.send_keys(prazo_final.strftime("%d/%m/%Y"))

    hora_field = encontrar_elemento_visivel(driver, By.NAME, "_dyORASCADANSLEG_A", timeout=config.WAIT_TIMEOUT)
    hora_field.clear()
    hora_field.send_keys(prazo_final.strftime("%H:%M"))

    # O título "Lista Atividades (N)" da checagem ANTERIOR já pode estar na
    # tela. Guarda a referência antes de clicar em "Busca" para depois
    # esperar ela ficar "stale" (o GWT recria o elemento) — sem isso a
    # espera logo abaixo passaria na hora, pegando o resultado ANTIGO.
    try:
        titulo_antigo = driver.find_element(By.XPATH, "//*[contains(text(),'Lista Atividades')]")
    except Exception:
        titulo_antigo = None

    try:
        esperar_e_clicar_visivel(driver, XPATH_BOTAO_BUSCA, timeout=45)
    except Exception:
        debug_screenshot(driver, "botao_busca_atividades_programaveis_nao_encontrado")
        raise

    if titulo_antigo is not None:
        try:
            WebDriverWait(driver, 45).until(EC.staleness_of(titulo_antigo))
        except Exception:
            pass  # se não detectar a troca a tempo, segue e tenta achar o novo mesmo assim

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
    Atividades" aparece, o que invalida (stale) os elementos capturados —
    por isso a extração inteira roda dentro de um retry.
    """
    ultimo_erro = None
    for _ in range(tentativas):
        try:
            tabela_resultados = None
            for t in driver.find_elements(By.CSS_SELECTOR, "table.tvGrid"):
                trs = t.find_elements(By.TAG_NAME, "tr")
                if trs and "Código TdC" in trs[0].text:
                    tabela_resultados = t
                    break

            if tabela_resultados is None:
                raise RuntimeError("Tabela de resultados não encontrada — verifique se a busca retornou dados.")

            registros = []
            for linha in tabela_resultados.find_elements(By.TAG_NAME, "tr")[1:]:  # pula cabeçalho
                # A grade sempre renderiza um número fixo de linhas de padding
                # (class "tvRowEmpty", células só com "&nbsp;").
                if "tvRowEmpty" in (linha.get_attribute("class") or ""):
                    continue

                celulas = [c.text.strip() for c in linha.find_elements(By.TAG_NAME, "td")]
                if len(celulas) < 12 or not celulas[1]:
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
                # Data/hora de vencimento = fim do intervalo, após "a "
                partes = registro["intervalo_execucao"].split("\na ")
                registro["vencimento"] = partes[1].strip() if len(partes) > 1 else None
                registros.append(registro)

            return registros

        except StaleElementReferenceException as e:
            ultimo_erro = e
            time.sleep(1)

    debug_screenshot(driver, "extracao_stale_apos_tentativas")
    raise RuntimeError(
        f"Não consegui extrair os registros após {tentativas} tentativas "
        f"(elementos ficaram stale repetidamente). Último erro: {ultimo_erro}"
    )


def obter_total_declarado(driver):
    """Total real a partir do título 'Lista Atividades (N)'.

    A grade mostra no máximo ~25 linhas por página, então len(registros) não
    é o total quando há mais de uma página.
    TODO: paginação, se for preciso o detalhe de TODOS os registros.
    """
    try:
        el = driver.find_element(By.XPATH, "//*[contains(text(),'Lista Atividades')]")
        m = re.search(r"Lista Atividades\s*\((\d+)\)", el.text)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None
