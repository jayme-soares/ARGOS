"""Os dois monitores do ARGOS, cada um numa thread com seu próprio Chrome.

Ficam separados porque a exportação da Busca TdC pode levar vários minutos
(o eOrder gera o arquivo de forma assíncrona) — num Chrome só, a checagem de
1 em 1 minuto dos programáveis ficaria parada esse tempo todo.

Modo sequencial (ARGOS_MODO_SEQUENCIAL=1): se o eOrder derrubar uma sessão
quando o mesmo usuário loga em outra, os monitores revezam o acesso via
LOCK_EORDER, e depois de cada exportação o monitor de programáveis descarta
a própria sessão (que o login do monitor de campo invalidou) e reloga.
"""

import contextlib
import threading
from datetime import datetime, timedelta

from argos import config, publicador, saude
from argos.alertas import PROGRAMAVEIS, formatar_duracao, linha_push, processar_alertas
from argos.eorder.busca_tdc import exportar_religas_em_campo
from argos.eorder.driver import abrir_driver, fechar_driver
from argos.eorder.programaveis import (
    extrair_registros,
    filtrar_municipio,
    navegar_ate_atividades_programaveis,
    obter_total_declarado,
    preencher_busca,
    proximo_dia_util,
)
from argos.eorder.sessao import login
from argos.eorder.ui import debug_screenshot
from argos.estado import carregar_json, salvar_json
from argos.log import log
from argos.notificacao import enviar_notificacao_push
from argos.planilha import ler_exportacao_campo

LOCK_EORDER = threading.Lock()
_sessao_programaveis_invalidada = threading.Event()


def dentro_da_janela_permitida(agora: datetime | None = None) -> bool:
    agora = agora or datetime.now(config.TIMEZONE)
    if agora.weekday() not in config.DIAS_SEMANA:
        return False
    return config.HORARIO_INICIO <= agora.hour < config.HORARIO_FIM


@contextlib.contextmanager
def _acesso_eorder(worker: str):
    """No modo sequencial, espera a vez de usar o eOrder batendo o watchdog
    (esperar a exportação do outro monitor não é travamento)."""
    if not config.MODO_SEQUENCIAL:
        yield
        return
    while not LOCK_EORDER.acquire(timeout=30):
        saude.bater(worker)
    try:
        yield
    finally:
        LOCK_EORDER.release()


def _agora() -> datetime:
    return datetime.now(config.TIMEZONE)


def _data_eorder_para_iso(texto: str | None) -> str | None:
    if not texto:
        return None
    try:
        return datetime.strptime(texto, "%d/%m/%Y %H:%M").replace(tzinfo=config.TIMEZONE).isoformat(timespec="minutes")
    except ValueError:
        return None


# ------------------------------------------------------------------
# PROGRAMÁVEIS
# ------------------------------------------------------------------

class MonitorProgramaveis(threading.Thread):
    NOME = "programaveis"
    PREFIXO = "PROGRAMÁVEIS"
    ARQUIVO_ESTADO = "programaveis.json"

    def __init__(self):
        super().__init__(name=self.NOME, daemon=True)

    def _status(self, estado):
        saude.atualizar_status(self.NOME, estado)

    def verificar_uma_vez(self, driver) -> str:
        """Busca, extrai, compara com a checagem anterior (para saber o que é
        novo), publica no painel e envia push só se entrou religação NOVA."""
        agora = _agora()
        preencher_busca(driver, proximo_dia_util(agora, dias_uteis=config.DIAS_UTEIS_PRAZO))

        brutos = extrair_registros(driver)
        registros = filtrar_municipio(brutos)
        quantidade = obter_total_declarado(driver)
        if quantidade is None or quantidade <= len(brutos):
            quantidade = len(registros)
        else:
            # Há outras páginas que não são lidas: o total do eOrder inclui
            # outros municípios; desconta ao menos os da primeira página.
            quantidade -= len(brutos) - len(registros)

        # Estado persistido no volume: um restart do container NÃO faz tudo
        # parecer novo. None só na primeira execução de todas.
        estado_anterior = carregar_json(self.ARQUIVO_ESTADO)
        anteriores = (estado_anterior or {}).get("registros", {})
        ids_novos = [r["codigo_tdc"] for r in registros if r["codigo_tdc"] not in anteriores]
        permanecem = len(registros) - len(ids_novos)

        for r in registros:
            r["vencimento_iso"] = _data_eorder_para_iso(r.get("vencimento"))
            r["primeiro_visto_em"] = anteriores.get(r["codigo_tdc"], {}).get("primeiro_visto_em") or agora.isoformat(timespec="seconds")

        salvar_json(self.ARQUIVO_ESTADO, {
            "quantidade_declarada": quantidade,
            "atualizado_em": agora.isoformat(timespec="seconds"),
            "registros": {r["codigo_tdc"]: r for r in registros},
        })

        publicador.atualizar_secao("programaveis", {
            "atualizado_em": agora.isoformat(timespec="seconds"),
            "total": quantidade,
            "registros": [
                {
                    "tdc": r["codigo_tdc"],
                    "ordem": r["numero_servico"],
                    "cliente": r["codigo_cliente_medidor"],
                    "endereco": r["endereco"],
                    "tipo": r["tipo_servico"],
                    "atividade": r["des_atividade"],
                    "vencimento": r["vencimento_iso"],
                    "primeiro_visto_em": r["primeiro_visto_em"],
                }
                for r in registros
            ],
        })

        self._avaliar_alertas_vencimento(registros, agora, parcial=quantidade > len(registros))

        vencimentos = [r["vencimento_iso"] for r in registros if r["vencimento_iso"]]
        venc_txt = ""
        if vencimentos:
            venc_txt = f" Vencimento mais próximo: {datetime.fromisoformat(min(vencimentos)):%d/%m/%Y %H:%M}."

        if not quantidade:
            return "Há 0 religações programáveis."
        if estado_anterior is None:
            return f"Há {quantidade} religações programáveis.{venc_txt}"
        if not ids_novos:
            return f"Continuam {quantidade} religações programáveis (nenhuma nova).{venc_txt}"

        texto = f"Entraram {len(ids_novos)} nova(s) religação(ões) programável(is)"
        if permanecem > 0:
            texto += f" (as {permanecem} anteriores continuam)"
        texto += f". Total: {quantidade}.{venc_txt}"

        novos = [r for r in registros if r["codigo_tdc"] in set(ids_novos)]
        linhas = [
            linha_push(r["codigo_tdc"], datetime.fromisoformat(r["vencimento_iso"]) if r["vencimento_iso"] else None)
            for r in novos[:5]
        ]
        if len(novos) > 5:
            linhas.append(f"+{len(novos) - 5} outra(s) — veja o painel.")
        enviar_notificacao_push(
            titulo=f"ARGOS · {len(ids_novos)} nova(s) religação(ões) programável(is)",
            mensagem="\n".join(linhas),
            prioridade=4,
            tags=["rotating_light"],
        )
        return texto

    def _avaliar_alertas_vencimento(self, registros, agora, parcial):
        """Pushes de vencimento das programáveis (2h, 1h, 30, 15 min e vencida).
        Uma falha aqui não pode derrubar a checagem nem o push de novas."""
        try:
            avisos = processar_alertas(
                [{"tdc": r["codigo_tdc"], "vencimento": r["vencimento_iso"]} for r in registros],
                agora,
                categoria=PROGRAMAVEIS,
                parcial=parcial,
            )
            for aviso in avisos:
                log(f"Alerta: {aviso['titulo']}", self.PREFIXO)
        except Exception as e:
            log(f"ERRO ao avaliar alertas de vencimento: {e}", self.PREFIXO)

    def run(self):
        """Só liga o navegador dentro da janela permitida. Recuperação em 3
        estágios após falhas seguidas: renavegar → relogar → descartar a
        sessão (o próximo ciclo abre um Chrome novo)."""
        driver = None
        falhas_consecutivas = 0
        try:
            while True:
                if not dentro_da_janela_permitida():
                    if driver is not None:
                        log("Fora do horário permitido — encerrando navegador.", self.PREFIXO)
                        fechar_driver(driver)
                        driver = None
                        falhas_consecutivas = 0
                    self._status("fora_do_horario")
                    saude.dormir(self.NOME, config.FORA_DA_JANELA_INTERVALO_SEGUNDOS)
                    continue

                with _acesso_eorder(self.NOME):
                    if _sessao_programaveis_invalidada.is_set():
                        _sessao_programaveis_invalidada.clear()
                        if driver is not None:
                            log("Sessão invalidada pela exportação de campo — relogando.", self.PREFIXO)
                            fechar_driver(driver)
                            driver = None

                    if driver is None:
                        log("Iniciando navegador e login.", self.PREFIXO)
                        self._status("iniciando_sessao")
                        try:
                            driver = abrir_driver()
                            login(driver)
                            navegar_ate_atividades_programaveis(driver)
                        except Exception as e:
                            log(f"Falha ao iniciar sessão: {e}", self.PREFIXO)
                            fechar_driver(driver)
                            driver = None
                            self._status(f"erro_login: {e}")
                            saude.dormir(self.NOME, config.INTERVALO_PROGRAMAVEIS_SEGUNDOS)
                            continue

                    try:
                        texto = self.verificar_uma_vez(driver)
                        falhas_consecutivas = 0
                        log(texto, self.PREFIXO)
                        self._status("ok")
                    except Exception as e:
                        falhas_consecutivas += 1
                        log(f"ERRO na verificação (falha {falhas_consecutivas} seguida(s)): {e}", self.PREFIXO)
                        debug_screenshot(driver, "erro_ciclo_programaveis")
                        self._status(f"erro: {e}")

                        if falhas_consecutivas >= config.MAX_FALHAS_CONSECUTIVAS_ANTES_DE_RECUPERAR:
                            driver, falhas_consecutivas = self._recuperar(driver, falhas_consecutivas)

                saude.dormir(self.NOME, config.INTERVALO_PROGRAMAVEIS_SEGUNDOS)
        finally:
            fechar_driver(driver)

    def _recuperar(self, driver, falhas):
        log(f"{falhas} falhas seguidas — tentando recuperar renavegando.", self.PREFIXO)
        try:
            navegar_ate_atividades_programaveis(driver)
            return driver, 0
        except Exception as e_nav:
            log(f"Renavegação falhou ({e_nav}); tentando relogar do zero.", self.PREFIXO)
            debug_screenshot(driver, "falha_recuperacao_navegacao")
        try:
            login(driver)
            navegar_ate_atividades_programaveis(driver)
            return driver, 0
        except Exception as e_login:
            log(f"Relogin também falhou ({e_login}); descartando sessão.", self.PREFIXO)
            debug_screenshot(driver, "falha_recuperacao_relogin")
            fechar_driver(driver)
            return None, falhas


# ------------------------------------------------------------------
# EM CAMPO
# ------------------------------------------------------------------

class MonitorCampo(threading.Thread):
    """A cada INTERVALO_CAMPO_MINUTOS abre um Chrome, loga, exporta a
    planilha da Busca TdC e fecha o Chrome. Abrir/fechar a cada exportação
    (em vez de manter a sessão aberta 30 min parada) evita lidar com sessão
    expirada e libera memória entre um ciclo e outro.

    Entre exportações, reavalia os alertas de vencimento a cada
    REAVALIACAO_ALERTAS_MINUTOS sobre os últimos dados lidos."""

    NOME = "campo"
    PREFIXO = "CAMPO"

    def __init__(self):
        super().__init__(name=self.NOME, daemon=True)
        secao = publicador.obter_secao("campo") or {}
        self.registros = secao.get("registros") or []
        self.finalizadas = secao.get("finalizadas") or []
        self.atualizado_em = datetime.fromisoformat(secao["atualizado_em"]) if secao.get("atualizado_em") else None
        self.arquivo = secao.get("arquivo")
        self.proxima_extracao = _agora()  # exporta assim que entrar na janela
        self._avisou_dados_velhos = False

    def _status(self, estado):
        saude.atualizar_status(self.NOME, estado)

    def _exportar(self):
        driver = None
        try:
            self._status("iniciando_sessao")
            driver = abrir_driver(pasta_download=config.DOWNLOAD_DIR)
            login(driver)
            return exportar_religas_em_campo(driver, progresso=lambda etapa: saude.bater(self.NOME))
        finally:
            fechar_driver(driver)
            if config.MODO_SEQUENCIAL:
                _sessao_programaveis_invalidada.set()

    def _publicar(self):
        publicador.atualizar_secao("campo", {
            "atualizado_em": self.atualizado_em.isoformat(timespec="seconds") if self.atualizado_em else None,
            "proxima_extracao": self.proxima_extracao.isoformat(timespec="seconds"),
            "arquivo": self.arquivo,
            "registros": self.registros,
            "finalizadas": self.finalizadas,
        })

    def _completar_finalizadas(self, finalizadas, agora):
        """Sem "Data Fim" na planilha, a hora de finalização fica sendo a da
        primeira exportação em que a ordem apareceu finalizada (guardada na
        seção do snapshot, então sobrevive a um restart)."""
        anteriores = {r["tdc"]: r.get("finalizada_em") for r in self.finalizadas}
        for r in finalizadas:
            if r["finalizada_em"]:
                continue
            r["finalizada_em"] = anteriores.get(r["tdc"]) or agora.isoformat(timespec="minutes")
            if r["vencimento"]:
                r["no_prazo"] = datetime.fromisoformat(r["finalizada_em"]) <= datetime.fromisoformat(r["vencimento"])
        finalizadas.sort(key=lambda r: r["finalizada_em"], reverse=True)
        return finalizadas

    def ciclo_exportacao(self):
        log("Iniciando exportação da Busca TdC.", self.PREFIXO)
        try:
            with _acesso_eorder(self.NOME):
                arquivo = self._exportar()
            dados = ler_exportacao_campo(arquivo)
        except Exception as e:
            self.proxima_extracao = _agora() + timedelta(minutes=config.RETENTATIVA_CAMPO_MINUTOS)
            log(f"ERRO na exportação/leitura: {e}. Nova tentativa às {self.proxima_extracao:%H:%M}.", self.PREFIXO)
            self._status(f"erro: {e}")
            return

        registros = dados["em_aberto"]
        self.registros = registros
        self.arquivo = arquivo.name
        self.atualizado_em = _agora()
        self.finalizadas = self._completar_finalizadas(dados["finalizadas"], self.atualizado_em)
        self.proxima_extracao = self.atualizado_em + timedelta(minutes=config.INTERVALO_CAMPO_MINUTOS)
        self._avisou_dados_velhos = False
        vencidas = sum(1 for r in registros if r["vencimento"] and datetime.fromisoformat(r["vencimento"]) <= self.atualizado_em)
        hoje = self.atualizado_em.date()
        finalizadas_hoje = sum(1 for r in self.finalizadas if datetime.fromisoformat(r["finalizada_em"]).date() == hoje)
        estados = ", ".join(f"{e or '(vazio)'}: {n}" for e, n in sorted(dados["estados"].items(), key=lambda x: -x[1]))
        log(
            f"{len(registros)} religa(s) em campo, {vencidas} vencida(s); {len(self.finalizadas)} finalizada(s), "
            f"{finalizadas_hoje} hoje. Estados na planilha: {estados or '-'}. "
            f"Próxima exportação às {self.proxima_extracao:%H:%M}.",
            self.PREFIXO,
        )
        self._publicar()
        self._status("ok")

    def avaliar_alertas(self):
        agora = _agora()
        if self.atualizado_em is None:
            return
        idade = agora - self.atualizado_em
        if idade > timedelta(minutes=config.IDADE_MAXIMA_DADOS_CAMPO_MINUTOS):
            if not self._avisou_dados_velhos:
                log(f"Dados de campo com {formatar_duracao(idade.total_seconds() / 60)} — alertas suspensos até a próxima exportação.", self.PREFIXO)
                self._avisou_dados_velhos = True
            return
        avisos = processar_alertas(self.registros, agora)
        for aviso in avisos:
            log(f"Alerta: {aviso['titulo']}", self.PREFIXO)

    def run(self):
        while True:
            if not dentro_da_janela_permitida():
                self._status("fora_do_horario")
                saude.dormir(self.NOME, config.FORA_DA_JANELA_INTERVALO_SEGUNDOS)
                continue

            if _agora() >= self.proxima_extracao:
                self.ciclo_exportacao()

            try:
                self.avaliar_alertas()
            except Exception as e:
                log(f"ERRO ao avaliar alertas: {e}", self.PREFIXO)

            espera = min(
                config.REAVALIACAO_ALERTAS_MINUTOS * 60,
                max(5.0, (self.proxima_extracao - _agora()).total_seconds()),
            )
            saude.bater(self.NOME)
            saude.dormir(self.NOME, espera)
