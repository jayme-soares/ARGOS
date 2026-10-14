"""Lista TdC > Busca TdC > filtro salvo "PARCIAL RELIGA CENEGED - MARICÁ" >
Centro Operativo > Exportar em xls > Lista de exportação > download.

O filtro salvo cobre todos os COs, mas a tela exige escolher um por busca:
por ciclo sai uma exportação por CO (SOC: "Valtellina - Niterói";
Negociação: cada "Ceneged - X"). Todas são pedidas primeiro e baixadas
depois, porque o eOrder gera os arquivos de forma assíncrona.

Portado de Producao SOC/modules/eorder_interact.py (export_data e
download_file), com estas correções:
- erros sobem como exceção (com screenshot) em vez de retornar None e o
  resto do fluxo seguir procurando um arquivo chamado "None";
- o download é confirmado de verdade (arquivo novo na pasta, sem
  .crdownload, tamanho estável), com timeout que vira erro;
- nome de exportação único e independente do locale (%B);
- XPaths absolutos só como último recurso, depois de seletores por
  texto/atributo.

Uso avulso (descoberta/diagnóstico, de preferência com ARGOS_HEADLESS=0):
    python -m argos.eorder.busca_tdc                          # SOC: exporta, baixa e imprime
    python -m argos.eorder.busca_tdc --contrato negociacao    # todos os COs da Negociação
    python -m argos.eorder.busca_tdc --contrato negociacao --co "Ceneged - Angra (CENEGED_ANGRA)"
    python -m argos.eorder.busca_tdc --arquivo X              # só lê uma planilha já baixada
"""

import time
from datetime import datetime, timedelta
from pathlib import Path

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select

from argos import config
from argos.eorder.ui import (
    XPATH_BOTAO_BUSCA,
    clicar_primeiro_disponivel,
    click_by_text,
    debug_screenshot,
    encontrar_elemento_visivel,
    esperar_e_clicar_visivel,
    selecionar_opcao_por_texto,
)
from argos.log import log

PREFIXO_LOG = "CAMPO"

XPATHS_MENU_LISTA_TDC = [
    "//div[contains(@class,'tbi') and normalize-space(.)='Lista TdC']",
    "//div[contains(@class,'tbi') and contains(normalize-space(.),'Lista TdC')]",
    "//div[not(*) and normalize-space(.)='Lista TdC']",
    "/html/body/div[1]/div[2]/table/tbody/tr[3]/td/div/div/table/tbody/tr[1]/td/div/div[2]/div[5]",
]
XPATH_BUSCA_TDC = "//div[contains(@onclick, '.10.0')]"
XPATH_LISTA_EXPORTACAO = "//div[contains(@onclick, '.10.3')]"
XPATHS_BOTAO_BUSCAR = [
    XPATH_BOTAO_BUSCA,
    "//button[contains(normalize-space(.),'Buscar')]",
    "/html/body/div[2]/div/div[2]/div/div[1]/div[2]/div[1]/div/form/div[2]/table/tbody/tr/td[2]/button",
]
# Ícone de "opções" do painel de resultados e da lista de exportação. Só
# temos o XPath absoluto mapeado no projeto do SOC.
XPATHS_MENU_OPCOES_RESULTADO = ["/html/body/div[2]/div/div[2]/div/div[3]/div[1]/div/div[1]/img"]
XPATHS_MENU_OPCOES_LISTA_EXPORTACAO = ["/html/body/div[2]/div/div[2]/div/div[1]/div/div[1]/img"]
XPATH_EXPORTAR_XLS = "//div[contains(text(), 'Exportar em xls')]"
XPATH_ATUALIZAR = "//div[contains(text(), 'Atualizar')]"
XPATH_CONFIRMAR_EXPORTACAO_ABSOLUTO = (
    "/html/body/div[2]/div/div[2]/div/div[16]/div/div[2]/div/div/div/div/div[2]/div/form/div[2]/table/tbody/tr/td[1]/button"
)

EXTENSOES_TEMPORARIAS = (".crdownload", ".tmp", ".part")


def _nada(_etapa: str):
    pass


def _esc(driver):
    ActionChains(driver).send_keys(Keys.ESCAPE).perform()


def limpar_downloads():
    """Apaga as planilhas de ciclos anteriores. A última baixada fica até o
    próximo ciclo (útil para conferir o que foi lido)."""
    config.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    for arq in config.DOWNLOAD_DIR.iterdir():
        try:
            if arq.is_file():
                arq.unlink()
        except Exception:
            pass


def abrir_busca_tdc(driver):
    try:
        clicar_primeiro_disponivel(driver, XPATHS_MENU_LISTA_TDC, timeout=45)
        time.sleep(2)
        esperar_e_clicar_visivel(driver, XPATH_BUSCA_TDC, timeout=45, via_js=True)
        time.sleep(5)
    except Exception:
        debug_screenshot(driver, "busca_tdc_nao_abriu")
        raise


def selecionar_filtro(driver, nome_filtro: str):
    try:
        sel = selecionar_opcao_por_texto(driver, nome_filtro, timeout=60)
        try:
            log(f"Filtro selecionado no combo name={sel.get_attribute('name')!r}: "
                f"{Select(sel).first_selected_option.text.strip()!r}", PREFIXO_LOG)
        except Exception:
            pass  # o GWT pode ter recriado o combo depois da troca
        # O GWT recarrega o formulário ao trocar a busca salva.
        time.sleep(3)
    except Exception:
        debug_screenshot(driver, "filtro_campo_nao_encontrado")
        raise


def _achar_input_por_id(driver, id_elemento: str):
    """O id mapeado pode ser do próprio <input> ou de um wrapper em volta dele."""
    el = encontrar_elemento_visivel(driver, By.XPATH, f"//*[@id='{id_elemento}']", timeout=5)
    if el.tag_name.lower() == "input":
        return el
    return el.find_element(By.XPATH, ".//input")


# Procura o rótulo "lançamento" (sem acento/caixa) e devolve os dois primeiros
# inputs de texto visíveis depois dele na ordem do documento, ignorando campos
# de hora. É o fallback para quando o GWT gera ids diferentes a cada sessão.
_JS_INPUTS_APOS_ROTULO = r"""
const norm = (t) => (t || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
const visivel = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
const rotulos = Array.from(document.querySelectorAll("td, span, div, label, legend"))
  .filter((el) => visivel(el) && el.children.length <= 1 && /lancamento/.test(norm(el.innerText)) && norm(el.innerText).length < 60);
for (const rotulo of rotulos) {
  const inputs = Array.from(document.querySelectorAll("input"))
    .filter((i) => visivel(i) && (!i.type || i.type === "text")
      && (rotulo.compareDocumentPosition(i) & Node.DOCUMENT_POSITION_FOLLOWING)
      && !/ORA|HORA/i.test(i.name || ""));
  if (inputs.length >= 2) return [inputs[0], inputs[1], rotulo.innerText];
}
return null;
"""


def _achar_inputs_data_lancamento(driver):
    try:
        return (
            _achar_input_por_id(driver, config.ID_DATA_LANCAMENTO_INICIO),
            _achar_input_por_id(driver, config.ID_DATA_LANCAMENTO_FIM),
        )
    except Exception:
        pass
    resultado = driver.execute_script(_JS_INPUTS_APOS_ROTULO)
    if resultado:
        inicio, fim, rotulo = resultado
        log(
            f"Datas de lançamento localizadas pelo rótulo {rotulo.strip()!r}: "
            f"id={inicio.get_attribute('id')!r}/name={inicio.get_attribute('name')!r} e "
            f"id={fim.get_attribute('id')!r}/name={fim.get_attribute('name')!r}",
            PREFIXO_LOG,
        )
        return inicio, fim
    raise TimeoutException("Nenhum rótulo 'lançamento' com dois campos de data depois dele.")


_JS_DIAGNOSTICO = r"""
const visivel = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
const txt = (el) => (el ? el.innerText || "" : "").replace(/\s+/g, " ").trim().slice(0, 70);
return Array.from(document.querySelectorAll("input, select, textarea"))
  .filter((el) => el.type !== "hidden")
  .map((el) => {
    const td = el.closest("td");
    const fs = el.closest("fieldset");
    return {
      tag: el.tagName, id: el.id, name: el.name, type: el.type, visivel: visivel(el),
      valor: el.tagName === "SELECT" ? txt(el.options[el.selectedIndex]) : el.value,
      rotulo: txt(td && td.previousElementSibling),
      fieldset: txt(fs && fs.querySelector("legend")),
    };
  });
"""


def salvar_diagnostico(driver, nome):
    """Grava em DATA_DIR/debug a lista de campos do formulário (id, name,
    rótulo, valor) e o HTML da página — para mapear seletores sem precisar
    abrir o DevTools no eOrder."""
    config.DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    carimbo = datetime.now(config.TIMEZONE).strftime("%Y%m%d_%H%M%S")
    try:
        campos = driver.execute_script(_JS_DIAGNOSTICO)
        linhas = [
            f"{c['tag']:<8} vis={str(c['visivel']):<5} id={c['id']!r:<22} name={c['name']!r:<32} "
            f"rotulo={c['rotulo']!r:<40} fieldset={c['fieldset']!r:<25} valor={c['valor']!r}"
            for c in campos
        ]
        arq = config.DEBUG_DIR / f"diagnostico_{nome}_{carimbo}.txt"
        arq.write_text("\n".join(linhas), encoding="utf-8")
        (config.DEBUG_DIR / f"diagnostico_{nome}_{carimbo}.html").write_text(driver.page_source, encoding="utf-8")
        log(f"Diagnóstico do formulário salvo em: {arq}", "DEBUG")
    except Exception as e:
        log(f"Não consegui salvar o diagnóstico: {e}", "DEBUG")


def preencher_data_lancamento(driver, inicio: datetime, fim: datetime):
    """A Busca TdC exige a "Data de lançamento" (início e fim)."""
    try:
        try:
            campo_inicio, campo_fim = _achar_inputs_data_lancamento(driver)
        except TimeoutException:
            # A seção de datas pode estar recolhida.
            try:
                click_by_text(driver, "Datas", tag="div", timeout=3)
                time.sleep(0.5)
            except Exception:
                pass
            campo_inicio, campo_fim = _achar_inputs_data_lancamento(driver)
    except Exception:
        debug_screenshot(driver, "data_lancamento_nao_encontrada")
        salvar_diagnostico(driver, "data_lancamento")
        raise RuntimeError(
            f"Não encontrei os campos de Data de lançamento (ids {config.ID_DATA_LANCAMENTO_INICIO}/"
            f"{config.ID_DATA_LANCAMENTO_FIM} nem pelo rótulo). Veja o diagnostico_*.txt em "
            f"{config.DEBUG_DIR}."
        )

    for campo, data in ((campo_inicio, inicio), (campo_fim, fim)):
        campo.clear()
        campo.send_keys(data.strftime("%d/%m/%Y"))
    time.sleep(1)


def buscar(driver):
    try:
        clicar_primeiro_disponivel(driver, XPATHS_BOTAO_BUSCAR, timeout=45)
    except Exception:
        debug_screenshot(driver, "botao_buscar_tdc_nao_encontrado")
        raise
    time.sleep(4)
    _esc(driver)


def solicitar_exportacao(driver, nome_exportacao: str, timeout=180):
    """Abre o menu de opções do resultado > "Exportar em xls", informa o
    nome e confirma. Tenta em loop porque o menu só responde depois que o
    resultado da busca termina de carregar."""
    fim = time.time() + timeout
    while True:
        try:
            clicar_primeiro_disponivel(driver, XPATHS_MENU_OPCOES_RESULTADO, timeout=5)
            time.sleep(1)
            esperar_e_clicar_visivel(driver, XPATH_EXPORTAR_XLS, timeout=8)
            break
        except TimeoutException:
            if time.time() > fim:
                debug_screenshot(driver, "exportar_xls_nao_encontrado")
                raise
            _esc(driver)
            time.sleep(3)

    try:
        campo_nome = encontrar_elemento_visivel(driver, By.NAME, "_syFILENAME", timeout=30)
        campo_nome.clear()
        campo_nome.send_keys(nome_exportacao)

        # Botão de confirmar: primeiro botão visível do mesmo formulário do
        # campo de nome (o XPath absoluto do SOC depende de div[16]).
        botao = None
        for b in campo_nome.find_elements(By.XPATH, "./ancestor::form[1]//button"):
            if b.is_displayed() and b.is_enabled():
                botao = b
                break
        if botao is not None:
            botao.click()
        else:
            esperar_e_clicar_visivel(driver, XPATH_CONFIRMAR_EXPORTACAO_ABSOLUTO, timeout=10)
    except Exception:
        debug_screenshot(driver, "confirmar_exportacao_falhou")
        raise
    time.sleep(3)
    _esc(driver)


def selecionar_co(driver, co_nome: str):
    """Combo Centro Operativo da Busca TdC (o filtro salvo deixa o
    "Valtellina - Niterói" selecionado)."""
    try:
        selecionar_opcao_por_texto(driver, co_nome, timeout=30)
        # O GWT pode recarregar o formulário ao trocar o CO.
        time.sleep(3)
    except Exception:
        debug_screenshot(driver, "co_busca_tdc_nao_encontrado")
        raise


def abrir_lista_exportacao(driver):
    try:
        esperar_e_clicar_visivel(driver, XPATH_LISTA_EXPORTACAO, timeout=45, via_js=True)
        time.sleep(3)
    except Exception:
        debug_screenshot(driver, "lista_exportacao_nao_abriu")
        raise


def _atualizar_lista_exportacao(driver):
    try:
        clicar_primeiro_disponivel(driver, XPATHS_MENU_OPCOES_LISTA_EXPORTACAO, timeout=10)
        time.sleep(1)
        esperar_e_clicar_visivel(driver, XPATH_ATUALIZAR, timeout=10)
    except TimeoutException:
        # Se o menu sumiu (painel fechou), reabrir a lista também atualiza.
        _esc(driver)
        abrir_lista_exportacao(driver)


def _arquivos_prontos(antes: set[str]) -> list[Path]:
    return [
        p for p in config.DOWNLOAD_DIR.iterdir()
        if p.is_file() and p.name not in antes and not p.name.lower().endswith(EXTENSOES_TEMPORARIAS)
    ]


def _download_em_andamento() -> bool:
    return any(p.name.lower().endswith(EXTENSOES_TEMPORARIAS) for p in config.DOWNLOAD_DIR.iterdir())


def esperar_download(antes: set[str], timeout: float, progresso=_nada) -> Path | None:
    """Espera surgir um arquivo novo e completo na pasta de download.
    Enquanto houver .crdownload, o prazo é estendido (o arquivo está
    chegando). Retorna None se nada começou a baixar dentro do prazo."""
    fim = time.time() + timeout
    limite_absoluto = time.time() + 10 * 60
    while time.time() < fim or (_download_em_andamento() and time.time() < limite_absoluto):
        progresso("baixando_planilha")
        prontos = _arquivos_prontos(antes)
        if prontos:
            arq = max(prontos, key=lambda p: p.stat().st_mtime)
            # Tamanho estável = Chrome terminou de gravar.
            tamanho = arq.stat().st_size
            time.sleep(2)
            if arq.exists() and arq.stat().st_size == tamanho and tamanho > 0:
                return arq
            continue
        time.sleep(1)
    return None


def baixar_exportacao(driver, nome_exportacao: str, progresso=_nada) -> Path:
    """Faz polling da Lista de exportação até a linha com `nome_exportacao`
    aparecer, dá duplo clique para baixar e confirma o arquivo na pasta."""
    fim = time.time() + config.EXPORTACAO_TIMEOUT_SEGUNDOS
    xpath_linha = f"//td[contains(., '{nome_exportacao}')]/.."
    while time.time() < fim:
        progresso("aguardando_exportacao")
        linha = None
        for el in driver.find_elements(By.XPATH, xpath_linha):
            try:
                if el.is_displayed():
                    linha = el
                    break
            except Exception:
                pass

        if linha is not None:
            antes = {p.name for p in config.DOWNLOAD_DIR.iterdir()}
            try:
                ActionChains(driver).move_to_element(linha).double_click().perform()
            except Exception as e:
                log(f"Duplo clique na exportação falhou ({e}); tentando de novo.", PREFIXO_LOG)
            arquivo = esperar_download(antes, timeout=60, progresso=progresso)
            if arquivo is not None:
                return arquivo
            log("Exportação listada, mas o download não começou (talvez ainda processando).", PREFIXO_LOG)
        else:
            log(f"Exportação ainda não disponível; nova verificação em {config.EXPORTACAO_POLLING_SEGUNDOS}s.", PREFIXO_LOG)

        time.sleep(config.EXPORTACAO_POLLING_SEGUNDOS)
        _atualizar_lista_exportacao(driver)

    debug_screenshot(driver, "exportacao_timeout")
    raise TimeoutError(
        f"A exportação '{nome_exportacao}' não ficou disponível/baixada em "
        f"{config.EXPORTACAO_TIMEOUT_SEGUNDOS}s."
    )


def _solicitar_exportacao_do_co(driver, co_nome: str | None, nome_exportacao: str, inicio: datetime,
                                fim: datetime, progresso=_nada):
    progresso("abrindo_busca_tdc")
    abrir_busca_tdc(driver)
    progresso("selecionando_filtro")
    selecionar_filtro(driver, config.FILTRO_CAMPO)
    if co_nome:
        selecionar_co(driver, co_nome)
    preencher_data_lancamento(driver, inicio, fim)
    progresso("buscando")
    buscar(driver)
    progresso("solicitando_exportacao")
    solicitar_exportacao(driver, nome_exportacao)


def exportar_cos(driver, cos: list[str], progresso=_nada) -> dict[str, Path | Exception]:
    """Pede uma exportação por CO e depois baixa todas. Com o driver já
    logado. Retorna {co: planilha baixada ou a exceção daquele CO}: um CO
    que falha não derruba os outros. `progresso(etapa)` é chamado a cada
    etapa (alimenta o watchdog e o status do painel)."""
    from argos.eorder.sessao import login

    agora = datetime.now(config.TIMEZONE)
    inicio = agora - timedelta(days=config.CAMPO_DIAS_ATRAS)
    limpar_downloads()

    resultado: dict[str, Path | Exception] = {}
    nomes: dict[str, str] = {}
    for i, co in enumerate(cos):
        nome = f"argos_campo_{agora:%Y%m%d_%H%M%S}_{i:02d}"
        try:
            _solicitar_exportacao_do_co(driver, co, nome, inicio, agora, progresso)
            nomes[co] = nome
            log(f"Exportação '{nome}' ({co}) solicitada.", PREFIXO_LOG)
        except Exception as e:
            resultado[co] = e
            log(f"ERRO ao pedir a exportação de {co}: {str(e).splitlines()[0]}", PREFIXO_LOG)
            # Recomeça do Menu Principal para o próximo CO não herdar a tela
            # quebrada. Ainda não há planilha baixada para o login apagar.
            try:
                login(driver)
            except Exception as e_login:
                log(f"Relogin após a falha também falhou: {e_login}", PREFIXO_LOG)
                for resto in cos[i + 1:]:
                    resultado[resto] = e_login
                break

    if nomes:
        abrir_lista_exportacao(driver)
    for co, nome in nomes.items():
        try:
            arquivo = baixar_exportacao(driver, nome, progresso=progresso)
            log(f"Planilha de {co} baixada: {arquivo.name} ({arquivo.stat().st_size} bytes)", PREFIXO_LOG)
            resultado[co] = arquivo
        except Exception as e:
            resultado[co] = e
            log(f"ERRO ao baixar a exportação de {co}: {str(e).splitlines()[0]}", PREFIXO_LOG)
    return resultado


if __name__ == "__main__":
    import argparse

    from argos.contratos import POR_ID
    from argos.eorder.driver import abrir_driver, fechar_driver
    from argos.eorder.sessao import login
    from argos.planilha import imprimir_exportacao, ler_exportacao_campo

    parser = argparse.ArgumentParser(description="Exporta e lê a planilha de religas em campo.")
    parser.add_argument("--arquivo", help="Só lê uma planilha já baixada, sem abrir o eOrder.")
    parser.add_argument("--contrato", choices=sorted(POR_ID), default="soc")
    parser.add_argument("--co", action="append", help="Só este(s) CO(s), pelo texto do combo.")
    args = parser.parse_args()
    contrato = POR_ID[args.contrato]

    if args.arquivo:
        planilhas = {None: Path(args.arquivo)}
    else:
        config.validar_config()
        drv = abrir_driver(pasta_download=config.DOWNLOAD_DIR)
        try:
            login(drv)
            planilhas = exportar_cos(drv, args.co or list(contrato.cos_campo),
                                     progresso=lambda etapa: log(etapa, PREFIXO_LOG))
        finally:
            fechar_driver(drv)

    for co, caminho in planilhas.items():
        print()
        print(f"===== {co or caminho} =====")
        if isinstance(caminho, Exception):
            print(f"FALHOU: {caminho}")
        else:
            imprimir_exportacao(ler_exportacao_campo(caminho, contrato, co))
