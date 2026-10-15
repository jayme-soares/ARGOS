"""Os monitores do ARGOS, cada um numa thread com seu próprio Chrome:

- um de programáveis por contrato (SOC: um CO a cada 1 min; Negociação:
  percorre os COs "Ceneged - X" em sequência, sem parar);
- um de campo para todos os contratos (uma exportação da Busca TdC por CO,
  todas no mesmo ciclo).

Ficam separados porque a exportação da Busca TdC pode levar vários minutos
(o eOrder gera o arquivo de forma assíncrona) — num Chrome só, a checagem de
1 em 1 minuto dos programáveis ficaria parada esse tempo todo.

Modo sequencial (ARGOS_MODO_SEQUENCIAL=1): se o eOrder derrubar uma sessão
quando o mesmo usuário loga em outra, os monitores revezam o acesso via
LOCK_EORDER, e depois de cada exportação o monitor de programáveis descarta
a própria sessão (que o login do monitor de campo invalidou) e reloga.
"""

import contextlib
import functools
import threading
from datetime import datetime, timedelta
from pathlib import Path

from argos import avisos_equipe, config, publicador, saude
from argos.alertas import PROGRAMAVEIS, formatar_duracao, linha_push, processar_alertas
from argos.contratos import Contrato
from argos.eorder.busca_tdc import exportar_cos
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
# Um evento por monitor de programáveis: o login do monitor de campo
# invalida a sessão de todos eles (modo sequencial).
_sessoes_invalidadas: list[threading.Event] = []


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
    """Programáveis de um contrato. Com um CO só (SOC), o Chrome fica na
    tela de Atividades Programáveis e só refaz a busca salva a cada checagem.
    Com vários (Negociação), cada checagem é uma volta completa: troca de CO
    (refaz a busca geral do Plano Diário), roda a busca salva do CO e extrai.
    Só no fim da volta publica, avalia os alertas e manda o push de novas."""

    def __init__(self, contrato: Contrato):
        self.contrato = contrato
        self.NOME = contrato.worker("programaveis")
        self.PREFIXO = "PROGRAMÁVEIS" if contrato.id == "soc" else f"PROGRAMÁVEIS {contrato.nome.upper()}"
        self.ARQUIVO_ESTADO = contrato.arquivo("programaveis.json")
        self.cos = list(contrato.programaveis)
        self._co_atual = None  # value do CO em que o Chrome está
        self._sessao_invalidada = threading.Event()
        _sessoes_invalidadas.append(self._sessao_invalidada)
        super().__init__(name=self.NOME, daemon=True)

    def _status(self, estado):
        saude.atualizar_status(self.NOME, estado, chave="programaveis", contratos=(self.contrato,))

    def _ir_para_co(self, driver, co):
        """Troca de CO. Com o Plano Diário já aberto, entrar de novo pelo
        menu cai em outra tela (testado na descoberta), então a troca parte
        do Menu Principal recém-carregado; se falhar, reloga e tenta de novo."""
        primeira = self._co_atual is None
        self._co_atual = None
        try:
            if not primeira:
                login(driver)
            navegar_ate_atividades_programaveis(driver, co.value)
        except Exception as e:
            log(f"Troca para o CO {co.nome} falhou ({str(e).splitlines()[0]}); relogando.", self.PREFIXO)
            login(driver)
            navegar_ate_atividades_programaveis(driver, co.value)
        self._co_atual = co.value

    def _ler_co(self, driver, co, prazo) -> tuple[list[dict], int, bool]:
        """(registros do CO, total declarado, leitura parcial?)."""
        if self._co_atual != co.value:
            self._ir_para_co(driver, co)
        saude.bater(self.NOME)
        preencher_busca(driver, prazo, co.busca)
        brutos = extrair_registros(driver)
        registros = brutos
        if self.contrato.municipio_programaveis:
            registros = filtrar_municipio(brutos, self.contrato.municipio_programaveis)
        quantidade = obter_total_declarado(driver)
        if quantidade is None or quantidade <= len(brutos):
            quantidade = len(registros)
        else:
            # Há outras páginas que não são lidas: o total do eOrder inclui
            # outros municípios; desconta ao menos os da primeira página.
            quantidade -= len(brutos) - len(registros)
        for r in registros:
            r["co"] = co.nome
        return registros, quantidade, quantidade > len(registros)

    def verificar_uma_vez(self, driver) -> str:
        """Busca, extrai, compara com a checagem anterior (para saber o que é
        novo), publica no painel e envia push só se entrou religação NOVA."""
        agora = _agora()
        prazo = proximo_dia_util(agora, dias_uteis=config.DIAS_UTEIS_PRAZO)

        # Estado persistido no volume: um restart do container NÃO faz tudo
        # parecer novo. None só na primeira execução de todas.
        estado_anterior = carregar_json(self.ARQUIVO_ESTADO)
        anteriores = (estado_anterior or {}).get("registros", {})

        registros, quantidade, parcial, falhas = [], 0, False, []
        total_por_co: dict[str, int] = {}
        for co in self.cos:
            try:
                regs, total, co_parcial = self._ler_co(driver, co, prazo)
            except Exception as e:
                if len(self.cos) == 1:
                    raise
                # Um CO com problema não para a volta: ficam os dados da volta
                # anterior dele, e a leitura conta como parcial (os alertas
                # não apagam o estado das ordens que não apareceram).
                falhas.append(co.nome)
                log(f"ERRO no CO {co.nome}: {str(e).splitlines()[0]}", self.PREFIXO)
                debug_screenshot(driver, f"erro_programaveis_{self.contrato.id}_{co.value}")
                self._co_atual = None
                regs = [r for r in anteriores.values() if r.get("co") == co.nome]
                total, co_parcial = len(regs), True
            registros += regs
            quantidade += total
            total_por_co[co.nome] = total
            parcial = parcial or co_parcial
        if falhas and len(falhas) == len(self.cos):
            raise RuntimeError(f"Nenhum CO foi lido ({', '.join(falhas)}).")

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
            # O painel da Negociação filtra os indicadores por CO.
            "total_por_co": total_por_co,
            "cos_com_erro": falhas,
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
                    "co": r.get("co"),
                }
                for r in registros
            ],
        }, self.contrato)

        self._avaliar_alertas_vencimento(registros, agora, parcial=parcial)

        vencimentos = [r["vencimento_iso"] for r in registros if r["vencimento_iso"]]
        venc_txt = ""
        if vencimentos:
            venc_txt = f" Vencimento mais próximo: {datetime.fromisoformat(min(vencimentos)):%d/%m/%Y %H:%M}."
        if falhas:
            venc_txt += f" CO(s) com erro nesta volta: {', '.join(falhas)}."

        if not quantidade:
            return f"Há 0 religações programáveis.{venc_txt}"
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
            titulo=f"{self.contrato.marca} · {len(ids_novos)} nova(s) religação(ões) programável(is)",
            mensagem="\n".join(linhas),
            prioridade=4,
            tags=["rotating_light"],
            contrato=self.contrato,
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
                contrato=self.contrato,
            )
            for aviso in avisos:
                log(f"Alerta: {aviso['titulo']}", self.PREFIXO)
        except Exception as e:
            log(f"ERRO ao avaliar alertas de vencimento: {e}", self.PREFIXO)

    def _abrir_sessao(self):
        driver = abrir_driver()
        try:
            login(driver)
            self._ir_para_co(driver, self.cos[0])
        except Exception:
            fechar_driver(driver)
            raise
        return driver

    def run(self):
        """Só liga o navegador dentro da janela permitida. Recuperação em 3
        estágios após falhas seguidas: renavegar → relogar → descartar a
        sessão (o próximo ciclo abre um Chrome novo)."""
        driver = None
        falhas_consecutivas = 0
        # Com vários COs a volta já leva minutos; não precisa esperar mais.
        intervalo = config.INTERVALO_PROGRAMAVEIS_SEGUNDOS if len(self.cos) == 1 else 5
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
                    if self._sessao_invalidada.is_set():
                        self._sessao_invalidada.clear()
                        if driver is not None:
                            log("Sessão invalidada pela exportação de campo — relogando.", self.PREFIXO)
                            fechar_driver(driver)
                            driver = None

                    if driver is None:
                        log("Iniciando navegador e login.", self.PREFIXO)
                        self._status("iniciando_sessao")
                        try:
                            driver = self._abrir_sessao()
                        except Exception as e:
                            log(f"Falha ao iniciar sessão: {e}", self.PREFIXO)
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
                        debug_screenshot(driver, f"erro_ciclo_programaveis_{self.contrato.id}")
                        self._status(f"erro: {e}")

                        if falhas_consecutivas >= config.MAX_FALHAS_CONSECUTIVAS_ANTES_DE_RECUPERAR:
                            driver, falhas_consecutivas = self._recuperar(driver, falhas_consecutivas)

                saude.dormir(self.NOME, intervalo)
        finally:
            fechar_driver(driver)

    def _recuperar(self, driver, falhas):
        log(f"{falhas} falhas seguidas — tentando recuperar renavegando.", self.PREFIXO)
        try:
            navegar_ate_atividades_programaveis(driver, self.cos[0].value)
            self._co_atual = self.cos[0].value
            return driver, 0
        except Exception as e_nav:
            log(f"Renavegação falhou ({e_nav}); tentando relogar do zero.", self.PREFIXO)
            debug_screenshot(driver, "falha_recuperacao_navegacao")
        try:
            login(driver)
            navegar_ate_atividades_programaveis(driver, self.cos[0].value)
            self._co_atual = self.cos[0].value
            return driver, 0
        except Exception as e_login:
            log(f"Relogin também falhou ({e_login}); descartando sessão.", self.PREFIXO)
            debug_screenshot(driver, "falha_recuperacao_relogin")
            fechar_driver(driver)
            return None, falhas


# ------------------------------------------------------------------
# EM CAMPO
# ------------------------------------------------------------------

class _CampoContrato:
    """Últimos dados de campo de um contrato (sobrevivem a um restart pela
    seção "campo" do snapshot local)."""

    def __init__(self, contrato: Contrato):
        self.contrato = contrato
        secao = publicador.obter_secao("campo", contrato) or {}
        self.registros = secao.get("registros") or []
        self.finalizadas = secao.get("finalizadas") or []
        self.atualizado_em = datetime.fromisoformat(secao["atualizado_em"]) if secao.get("atualizado_em") else None
        self.arquivo = secao.get("arquivo")
        self.cos_com_erro: list[str] = []
        self.avisou_dados_velhos = False

    @property
    def multi_co(self) -> bool:
        return len(self.contrato.cos_campo) > 1


class MonitorCampo(threading.Thread):
    """A cada INTERVALO_CAMPO_MINUTOS abre um Chrome, loga, exporta uma
    planilha da Busca TdC por CO (de todos os contratos) e fecha o Chrome.
    Abrir/fechar a cada ciclo (em vez de manter a sessão aberta 30 min
    parada) evita lidar com sessão expirada e libera memória entre um ciclo
    e outro.

    Entre exportações, reavalia os alertas de vencimento de cada contrato a
    cada REAVALIACAO_ALERTAS_MINUTOS sobre os últimos dados lidos."""

    NOME = "campo"
    PREFIXO = "CAMPO"

    def __init__(self, contratos: tuple[Contrato, ...]):
        super().__init__(name=self.NOME, daemon=True)
        self.contratos = tuple(c for c in contratos if c.cos_campo)
        self.dados = {c.id: _CampoContrato(c) for c in self.contratos}
        self.proxima_extracao = _agora()  # exporta assim que entrar na janela

    def _status(self, estado, contratos=None):
        saude.atualizar_status(self.NOME, estado, chave="campo", contratos=contratos or self.contratos)

    def _exportar(self) -> dict[str, Path | Exception]:
        cos = [co for c in self.contratos for co in c.cos_campo]
        driver = None
        try:
            self._status("iniciando_sessao")
            driver = abrir_driver(pasta_download=config.DOWNLOAD_DIR)
            login(driver)
            return exportar_cos(driver, cos, progresso=lambda etapa: saude.bater(self.NOME))
        finally:
            fechar_driver(driver)
            if config.MODO_SEQUENCIAL:
                for evento in _sessoes_invalidadas:
                    evento.set()

    def _publicar(self, d: _CampoContrato):
        publicador.atualizar_secao("campo", {
            "atualizado_em": d.atualizado_em.isoformat(timespec="seconds") if d.atualizado_em else None,
            "proxima_extracao": self.proxima_extracao.isoformat(timespec="seconds"),
            "arquivo": d.arquivo,
            "cos_com_erro": d.cos_com_erro,
            "registros": d.registros,
            "finalizadas": d.finalizadas,
        }, d.contrato)

    @staticmethod
    def _completar_finalizadas(d: _CampoContrato, finalizadas, agora):
        """Sem "Data Fim" na planilha, a hora de finalização fica sendo a da
        primeira exportação em que a ordem apareceu finalizada (guardada na
        seção do snapshot, então sobrevive a um restart)."""
        anteriores = {r["tdc"]: r.get("finalizada_em") for r in d.finalizadas}
        for r in finalizadas:
            if r["finalizada_em"]:
                continue
            r["finalizada_em"] = anteriores.get(r["tdc"]) or agora.isoformat(timespec="minutes")
            if r["vencimento"]:
                r["no_prazo"] = datetime.fromisoformat(r["finalizada_em"]) <= datetime.fromisoformat(r["vencimento"])
        finalizadas.sort(key=lambda r: r["finalizada_em"], reverse=True)
        return finalizadas

    def _ler_contrato(self, d: _CampoContrato, planilhas: dict[str, Path | Exception], agora) -> bool:
        """Lê as planilhas dos COs do contrato. Os COs que falharam mantêm os
        dados da exportação anterior. Retorna False se nenhum CO foi lido."""
        contrato = d.contrato
        lidos, falhas, estados, arquivos = {}, [], {}, []
        for co in contrato.cos_campo:
            arq = planilhas.get(co)
            if not isinstance(arq, Path):
                falhas.append(co)
                continue
            try:
                lidos[co] = ler_exportacao_campo(arq, contrato, co if d.multi_co else None)
                arquivos.append(arq.name)
                for e, n in lidos[co]["estados"].items():
                    estados[e] = estados.get(e, 0) + n
            except Exception as e:
                falhas.append(co)
                log(f"ERRO ao ler a planilha de {co} ({contrato.nome}): {e}", self.PREFIXO)
        if not lidos:
            log(f"[{contrato.nome}] Nenhuma planilha lida; os dados anteriores continuam no painel.", self.PREFIXO)
            return False

        registros = [r for dados in lidos.values() for r in dados["em_aberto"]]
        finalizadas = [r for dados in lidos.values() for r in dados["finalizadas"]]
        registros += [r for r in d.registros if r.get("co") in falhas]
        finalizadas += [r for r in d.finalizadas if r.get("co") in falhas]
        registros.sort(key=lambda r: (r["vencimento"] is None, r["vencimento"] or ""))

        d.registros = registros
        d.arquivo = ", ".join(arquivos)
        d.atualizado_em = agora
        d.finalizadas = self._completar_finalizadas(d, finalizadas, agora)
        d.cos_com_erro = falhas
        d.avisou_dados_velhos = False

        vencidas = sum(1 for r in registros if r["vencimento"] and datetime.fromisoformat(r["vencimento"]) <= agora)
        finalizadas_hoje = sum(1 for r in d.finalizadas if datetime.fromisoformat(r["finalizada_em"]).date() == agora.date())
        estados_txt = ", ".join(f"{e or '(vazio)'}: {n}" for e, n in sorted(estados.items(), key=lambda x: -x[1]))
        log(
            f"[{contrato.nome}] {len(registros)} religa(s) em campo, {vencidas} vencida(s); "
            f"{len(d.finalizadas)} finalizada(s), {finalizadas_hoje} hoje. Estados na planilha: {estados_txt or '-'}."
            + (f" CO(s) com erro: {', '.join(falhas)}." if falhas else ""),
            self.PREFIXO,
        )
        return True

    def ciclo_exportacao(self):
        log("Iniciando exportação da Busca TdC.", self.PREFIXO)
        try:
            with _acesso_eorder(self.NOME):
                planilhas = self._exportar()
        except Exception as e:
            self.proxima_extracao = _agora() + timedelta(minutes=config.RETENTATIVA_CAMPO_MINUTOS)
            log(f"ERRO na exportação: {e}. Nova tentativa às {self.proxima_extracao:%H:%M}.", self.PREFIXO)
            self._status(f"erro: {e}")
            return

        agora = _agora()
        lidos = {cid: self._ler_contrato(d, planilhas, agora) for cid, d in self.dados.items()}
        if not any(lidos.values()):
            self.proxima_extracao = agora + timedelta(minutes=config.RETENTATIVA_CAMPO_MINUTOS)
            erros = {str(e).splitlines()[0] for e in planilhas.values() if isinstance(e, Exception)}
            log(f"Nenhuma planilha lida ({'; '.join(erros) or 'sem detalhe'}). "
                f"Nova tentativa às {self.proxima_extracao:%H:%M}.", self.PREFIXO)
            self._status("erro: nenhuma planilha lida")
            return

        # Um CO falhando de vez não pode fazer o ciclo inteiro (12 exportações)
        # rodar a cada 5 min: a retentativa curta é só quando nada foi lido.
        self.proxima_extracao = agora + timedelta(minutes=config.INTERVALO_CAMPO_MINUTOS)
        log(f"Próxima exportação às {self.proxima_extracao:%H:%M}.", self.PREFIXO)
        for cid, d in self.dados.items():
            self._publicar(d)
            if not lidos[cid]:
                self._status("erro: nenhuma planilha lida", (d.contrato,))
                continue
            self._status(f"erro parcial: {', '.join(d.cos_com_erro)}" if d.cos_com_erro else "ok", (d.contrato,))
            try:
                avisos_equipe.avisar_designacoes(d.registros, d.contrato)
            except Exception as e:
                log(f"ERRO ao avisar designações às equipes ({d.contrato.nome}): {e}", self.PREFIXO)

    def avaliar_alertas(self):
        agora = _agora()
        for d in self.dados.values():
            if d.atualizado_em is None:
                continue
            idade = agora - d.atualizado_em
            if idade > timedelta(minutes=config.IDADE_MAXIMA_DADOS_CAMPO_MINUTOS):
                if not d.avisou_dados_velhos:
                    log(f"[{d.contrato.nome}] Dados de campo com {formatar_duracao(idade.total_seconds() / 60)} — "
                        "alertas suspensos até a próxima exportação.", self.PREFIXO)
                    d.avisou_dados_velhos = True
                continue
            try:
                avisos = processar_alertas(
                    d.registros, agora, contrato=d.contrato,
                    ao_disparar=functools.partial(avisos_equipe.avisar_vencimentos, contrato=d.contrato),
                )
                for aviso in avisos:
                    log(f"Alerta: {aviso['titulo']}", self.PREFIXO)
                avisos_equipe.reenviar_pendentes(d.registros, agora, d.contrato)
            except Exception as e:
                log(f"ERRO ao avaliar alertas ({d.contrato.nome}): {e}", self.PREFIXO)

    def run(self):
        while True:
            if not dentro_da_janela_permitida():
                self._status("fora_do_horario")
                saude.dormir(self.NOME, config.FORA_DA_JANELA_INTERVALO_SEGUNDOS)
                continue

            if _agora() >= self.proxima_extracao:
                self.ciclo_exportacao()

            self.avaliar_alertas()

            espera = min(
                config.REAVALIACAO_ALERTAS_MINUTOS * 60,
                max(5.0, (self.proxima_extracao - _agora()).total_seconds()),
            )
            saude.bater(self.NOME)
            saude.dormir(self.NOME, espera)
