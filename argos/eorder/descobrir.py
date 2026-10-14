"""Descoberta dos Centros Operativos e buscas salvas da Negociação.

Uso (de preferência com o Chrome visível, ARGOS_HEADLESS=0):
    python -m argos.eorder.descobrir                 # tudo
    python -m argos.eorder.descobrir --so-campo      # só os COs da Busca TdC
    python -m argos.eorder.descobrir --co 140        # só um CO do Plano Diário

Para cada CO "Ceneged - X (CENEGED_X)" do Plano Diário: seleciona o CO, roda
a busca geral, abre Atividades Programáveis e lista as buscas salvas
(_lyXWFMARSAID) visíveis para o usuário do bot. Na Busca TdC, com o filtro
salvo de campo, lista os textos exatos do combo Centro Operativo.

O resultado vai para DATA_DIR/debug/descoberta.json. Com ele se monta
ARGOS_NEG_PROGRAMAVEIS (value do CO : value da busca salva) e
ARGOS_NEG_COS_CAMPO. O formulário da primeira aba de Atividades Programáveis
também é salvo (diagnostico_*.txt) para mapear campos se faltar busca salva.
"""

import argparse
import json
import re

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select

from argos import config
from argos.eorder.busca_tdc import abrir_busca_tdc, salvar_diagnostico, selecionar_filtro
from argos.eorder.driver import abrir_driver, fechar_driver
from argos.eorder.programaveis import abrir_plano_diario, navegar_ate_atividades_programaveis
from argos.eorder.sessao import login
from argos.eorder.ui import debug_screenshot, encontrar_elemento_visivel, normalizar_texto
from argos.log import log

PREFIXO_LOG = "DESCOBERTA"
# "Ceneged - Angra (CENEGED_ANGRA)", "Ceneged - Petrópolis" (esse sem sigla).
# O "CENEGED - SOC MARICÁ" fica de fora.
PADRAO_CO_NEGOCIACAO = re.compile(r"^ceneged - (?!soc\b).+")


def eh_co_negociacao(texto: str) -> bool:
    return bool(PADRAO_CO_NEGOCIACAO.match(normalizar_texto(texto)))


def _opcoes(select_el) -> list[dict]:
    return [
        {"value": o.get_attribute("value"), "texto": " ".join(o.text.split())}
        for o in select_el.find_elements(By.TAG_NAME, "option")
        if (o.get_attribute("value") or "").strip()
    ]


def cos_plano_diario(driver) -> list[dict]:
    abrir_plano_diario(driver)
    sel = encontrar_elemento_visivel(driver, By.NAME, "_lyAUTEID_AFIL", timeout=config.WAIT_TIMEOUT)
    return _opcoes(sel)


def buscas_salvas_do_co(driver, co_value: str, diagnostico: bool) -> list[dict]:
    navegar_ate_atividades_programaveis(driver, co_value)
    sel = encontrar_elemento_visivel(driver, By.NAME, "_lyXWFMARSAID", timeout=config.WAIT_TIMEOUT)
    if diagnostico:
        salvar_diagnostico(driver, f"programaveis_co_{co_value}")
    return _opcoes(sel)


def cos_busca_tdc(driver) -> list[dict]:
    """Opções do combo Centro Operativo da Busca TdC (o select visível que
    tem alguma opção "(CENEGED_...)")."""
    abrir_busca_tdc(driver)
    selecionar_filtro(driver, config.FILTRO_CAMPO)
    for sel in driver.find_elements(By.TAG_NAME, "select"):
        try:
            if not sel.is_displayed():
                continue
            opcoes = _opcoes(sel)
            if any("(CENEGED_" in o["texto"].upper() for o in opcoes):
                try:
                    atual = Select(sel).first_selected_option.text.strip()
                except Exception:
                    atual = None
                return [dict(o, selecionado=(o["texto"] == atual)) for o in opcoes]
        except Exception:
            continue
    salvar_diagnostico(driver, "busca_tdc_cos")
    raise RuntimeError("Nenhum combo da Busca TdC com Centros Operativos CENEGED_.")


def main():
    parser = argparse.ArgumentParser(description="Descobre COs e buscas salvas da Negociação no eOrder.")
    parser.add_argument("--so-campo", action="store_true", help="Só os COs da Busca TdC.")
    parser.add_argument("--co", action="append", help="Só este(s) CO(s) do Plano Diário: value ou trecho do nome.")
    args = parser.parse_args()

    config.validar_config()
    resultado = {"plano_diario": [], "busca_tdc": []}
    driver = abrir_driver(pasta_download=config.DOWNLOAD_DIR)
    try:
        login(driver)
        if not args.so_campo:
            cos = [c for c in cos_plano_diario(driver) if eh_co_negociacao(c["texto"])]
            if args.co:
                alvos = [normalizar_texto(a) for a in args.co]
                cos = [c for c in cos if c["value"] in args.co
                       or any(a in normalizar_texto(c["texto"]) for a in alvos)]
            log(f"{len(cos)} CO(s) da Negociação no Plano Diário: {[c['texto'] for c in cos]}", PREFIXO_LOG)
            for i, co in enumerate(cos):
                # Cada CO parte do Menu Principal recém-carregado, como a
                # primeira volta do bot: não herda a tela do anterior.
                login(driver)
                item = dict(co)
                try:
                    item["buscas_salvas"] = buscas_salvas_do_co(driver, co["value"], diagnostico=(i == 0))
                    log(f"{co['texto']}: {[b['texto'] for b in item['buscas_salvas']]}", PREFIXO_LOG)
                except Exception as e:
                    item["erro"] = str(e).splitlines()[0]
                    log(f"{co['texto']}: ERRO {item['erro']}", PREFIXO_LOG)
                    debug_screenshot(driver, f"descoberta_co_{co['value']}")
                resultado["plano_diario"].append(item)
            login(driver)

        resultado["busca_tdc"] = cos_busca_tdc(driver)
        log(f"Busca TdC: {[c['texto'] for c in resultado['busca_tdc'] if eh_co_negociacao(c['texto'])]}", PREFIXO_LOG)
    finally:
        fechar_driver(driver)
        config.DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        arq = config.DEBUG_DIR / "descoberta.json"
        arq.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"Resultado salvo em {arq}", PREFIXO_LOG)


if __name__ == "__main__":
    main()
