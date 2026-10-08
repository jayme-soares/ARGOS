"use strict";

// ARGOS — painel de religações. Lê /api/snapshot (publicado pelo bot no
// Upstash) a cada 60s e recalcula a urgência no navegador a cada 30s, para
// as cores e contagens regressivas andarem entre uma leitura e outra.
//
// Login pelo Supabase Auth (usuários da empresa). O acesso ao painel depende
// da aprovação de um admin na tabela argos_acessos — ver supabase/migrations.

const TZ = "America/Sao_Paulo";
const INTERVALO_BUSCA_MS = 60_000;
const INTERVALO_RELOGIO_MS = 30_000;
const INTERVALO_CONTADOR_MS = 1_000;
const CHAVE_PREFS = "argos.prefs";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// ------------------------------------------------------------------
// ARMAZENAMENTO (pode falhar em aba anônima / site data bloqueado)
// ------------------------------------------------------------------
function lerStorage(storage, chave) {
  try { return storage.getItem(chave); } catch { return null; }
}
function gravarStorage(storage, chave, valor) {
  try { valor == null ? storage.removeItem(chave) : storage.setItem(chave, valor); } catch { /* ignora */ }
}

// ------------------------------------------------------------------
// ESTADO
// ------------------------------------------------------------------
const prefsSalvas = (() => {
  try { return JSON.parse(lerStorage(localStorage, CHAVE_PREFS) || "{}"); } catch { return {}; }
})();

const estado = {
  snapshot: null,
  aba: prefsSalvas.aba || "campo",
  texto: "",
  equipe: "",
  bairro: "",
  urg: "",
  periodo: "hoje",   // aba Finalizadas: "hoje" ou "" (toda a janela da extração)
  ordem: {
    campo: { col: "restante", dir: 1 },
    programaveis: { col: "restante", dir: 1 },
    finalizadas: { col: "finalizada", dir: -1 },
    equipes: { col: "vencidas", dir: -1 },
    acessos: { col: "status", dir: 1 },
  },
  sb: null,          // cliente Supabase
  acesso: null,      // linha do usuário em argos_acessos
  acessos: [],       // todas as linhas (só admins)
  ultimoErro: null,
  push: null,        // estado do sino — ver atualizarEstadoPush()
  pushSincronizado: false,
};
const ehAdmin = () => estado.acesso?.papel === "admin" && estado.acesso?.status === "aprovado";

function salvarPrefs() {
  gravarStorage(localStorage, CHAVE_PREFS, JSON.stringify({ aba: estado.aba, tema: document.documentElement.dataset.theme || null }));
}

// ------------------------------------------------------------------
// DATAS / FORMATAÇÃO
// ------------------------------------------------------------------
const fmtHora = new Intl.DateTimeFormat("pt-BR", { timeZone: TZ, hour: "2-digit", minute: "2-digit" });
const fmtDiaHora = new Intl.DateTimeFormat("pt-BR", { timeZone: TZ, day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
const fmtDia = new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" });
const fmtPartes = new Intl.DateTimeFormat("en-US", { timeZone: TZ, weekday: "short", hour: "numeric", hourCycle: "h23" });

function data(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return isNaN(d) ? null : d;
}
function mesmoDia(a, b) { return fmtDia.format(a) === fmtDia.format(b); }
function diaHora(d) {
  return d ? fmtDiaHora.format(d).replace(",", "") : "—";
}
// Contagem regressiva hh:mm:ss (horas podem passar de 24).
function cronometro(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return [h, m, s].map((n) => String(n).padStart(2, "0")).join(":");
}
function duracao(min) {
  const total = Math.round(Math.abs(min));
  const d = Math.floor(total / 1440);
  const h = Math.floor((total % 1440) / 60);
  const m = total % 60;
  if (d) return h ? `${d}d ${h}h` : `${d}d`;
  if (h) return m ? `${h}h${String(m).padStart(2, "0")}` : `${h}h`;
  return `${m} min`;
}
function textoRestante(min) {
  if (min == null) return "sem prazo";
  return min <= 0 ? `vencida há ${duracao(min)}` : `em ${duracao(min)}`;
}
function haQuanto(d, agora) {
  if (!d) return "nunca";
  const min = (agora - d) / 60000;
  return min < 1 ? "agora" : `há ${duracao(min)}`;
}

const DIAS_SEMANA = { Mon: 0, Tue: 1, Wed: 2, Thu: 3, Fri: 4, Sat: 5, Sun: 6 };
function dentroDaJanela(agora, janela) {
  if (!janela) return true;
  const partes = Object.fromEntries(fmtPartes.formatToParts(agora).map((p) => [p.type, p.value]));
  const dia = DIAS_SEMANA[partes.weekday];
  const hora = Number(partes.hour);
  return janela.dias_semana.includes(dia) && hora >= janela.inicio && hora < janela.fim;
}

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ------------------------------------------------------------------
// URGÊNCIA
// ------------------------------------------------------------------
function classificar(min) {
  if (min == null) return "sem";
  if (min <= 0) return "vencida";
  if (min <= 30) return "critico";
  if (min <= 60) return "alerta";
  if (min <= 120) return "atencao";
  return "ok";
}
// Os chips de urgência são cumulativos: "≤ 1h" inclui as de ≤ 30 min.
const FILTRO_URGENCIA = {
  vencida: (m) => m != null && m <= 0,
  critico: (m) => m != null && m > 0 && m <= 30,
  alerta: (m) => m != null && m > 0 && m <= 60,
  atencao: (m) => m != null && m > 0 && m <= 120,
};

function enriquecer(registros, agora) {
  return (registros || []).map((r) => {
    const venc = data(r.vencimento);
    const min = venc ? (venc - agora) / 60000 : null;
    return { ...r, _venc: venc, _min: min, _urg: classificar(min) };
  });
}

// Finalizadas: a cor da linha diz se fechou no prazo (verde) ou depois do
// vencimento (vermelho). _folga = minutos entre a finalização e o prazo.
function enriquecerFinalizadas(registros) {
  return (registros || []).map((r) => {
    const venc = data(r.vencimento);
    const fim = data(r.finalizada_em);
    const folga = venc && fim ? (venc - fim) / 60000 : null;
    return { ...r, _venc: venc, _fim: fim, _min: null, _folga: folga, _urg: r.no_prazo === true ? "ok" : r.no_prazo === false ? "vencida" : "sem" };
  });
}
const finalizadaHoje = (r, agora) => r._fim && mesmoDia(r._fim, agora);
const ABAS_COM_EQUIPE = new Set(["campo", "finalizadas"]);

// ------------------------------------------------------------------
// API
// ------------------------------------------------------------------
async function tokenAtual() {
  const { data } = await estado.sb.auth.getSession();
  return data.session?.access_token || null;
}

async function buscarSnapshot() {
  if (!estado.acesso) return false;
  const btn = $("#btn-atualizar");
  btn.classList.add("girando");
  try {
    const token = await tokenAtual();
    if (!token) { await sair("Sessão expirada. Entre novamente."); return false; }
    const r = await fetch("/api/snapshot", { headers: { Authorization: `Bearer ${token}` }, cache: "no-store" });
    const corpo = await r.json().catch(() => ({}));
    if (r.status === 401) { await sair(corpo.erro || "Sessão expirada. Entre novamente."); return false; }
    if (r.status === 403) { await verificarAcesso(); return false; }  // revogado enquanto usava
    if (!r.ok) throw new Error(corpo.erro || `Erro ${r.status}`);
    estado.snapshot = corpo;
    estado.ultimoErro = null;
    if (!estado.pushSincronizado) { estado.pushSincronizado = true; sincronizarPush(); }
    if (ehAdmin()) await carregarAcessos();
    return true;
  } catch (e) {
    estado.ultimoErro = e.message || String(e);
    return false;
  } finally {
    btn.classList.remove("girando");
    renderizar();
  }
}

async function carregarAcessos() {
  const { data, error } = await estado.sb
    .from("argos_acessos")
    .select("*")
    .order("solicitado_em", { ascending: false });
  if (!error) estado.acessos = data || [];
}

async function acaoAcesso(funcao, params) {
  const { error } = await estado.sb.rpc(funcao, params);
  if (error) { alert(error.message); renderizar(); return; }
  // A ação pode ter mudado o próprio acesso (ex.: deixou de ser admin).
  await verificarAcesso({ silencioso: true });
  if (ehAdmin()) await carregarAcessos();
  renderizar();
}

// ------------------------------------------------------------------
// LOGIN / APROVAÇÃO
// ------------------------------------------------------------------
function mostrarTela(id) {
  for (const t of ["#tela-login", "#tela-espera", "#tela-painel"]) $(t).hidden = t !== id;
}

function mostrarLogin(mensagem) {
  pararTimers();
  mostrarTela("#tela-login");
  $("#login-erro").textContent = mensagem || "";
  $("#login-email").focus();
}

function mostrarEspera(status, email) {
  pararTimers();
  mostrarTela("#tela-espera");
  const recusado = status === "recusado";
  $("#espera-titulo").textContent = recusado ? "Acesso não liberado" : "Aguardando aprovação";
  $("#espera-texto").textContent = recusado
    ? "Seu acesso ao ARGOS foi recusado ou revogado por um administrador. Fale com a coordenação se achar que é um engano."
    : "Seu pedido de acesso foi registrado. Assim que um administrador aprovar, é só clicar em “Verificar novamente”.";
  $("#espera-email").textContent = email || "";
}

async function sair(mensagem) {
  // Aparelho compartilhado: quem sai não continua recebendo os avisos.
  await desativarPush({ silencioso: true });
  estado.acesso = null;
  estado.snapshot = null;
  estado.acessos = [];
  try { await estado.sb.auth.signOut(); } catch { /* sessão já inválida */ }
  mostrarLogin(mensagem);
}

// Cria a solicitação no primeiro login (o primeiro usuário de todos vira
// admin) e decide qual tela mostrar.
async function verificarAcesso({ silencioso = false } = {}) {
  const { data: sessao } = await estado.sb.auth.getSession();
  if (!sessao.session) { mostrarLogin(); return; }
  const { data, error } = await estado.sb.rpc("argos_solicitar_acesso");
  if (error) {
    if (!silencioso) mostrarLogin(`Não foi possível verificar o acesso: ${error.message}`);
    return;
  }
  estado.acesso = data;
  if (data?.status === "aprovado") {
    if ($("#tela-painel").hidden) iniciarPainel();
  } else {
    mostrarEspera(data?.status, sessao.session.user.email);
  }
}

// O mesmo cartão serve para entrar e para criar conta. Os campos só do
// cadastro ficam desabilitados no modo "entrar" para não travar a validação.
let modoCadastro = false;
function definirModoCadastro(ativo) {
  modoCadastro = ativo;
  for (const el of $$("[data-so-cadastro]")) {
    el.hidden = !ativo;
    el.querySelector("input").disabled = !ativo;
  }
  $("#login-senha").autocomplete = ativo ? "new-password" : "current-password";
  $("#login-senha").minLength = ativo ? 6 : 0;
  $("#btn-login").textContent = ativo ? "Criar conta" : "Entrar";
  $("#btn-alternar-cadastro").textContent = ativo ? "Já tenho conta: entrar" : "Não tem conta? Cadastre-se";
  $("#login-dica").textContent = ativo
    ? "Depois do cadastro, um administrador precisa aprovar seu acesso."
    : "Se já usa os sistemas da empresa, entre com o mesmo email e senha. No primeiro acesso, um administrador precisa aprovar sua entrada.";
  $("#login-erro").textContent = "";
  (ativo ? $("#login-nome") : $("#login-email")).focus();
}
definirModoCadastro(false);

$("#btn-alternar-cadastro").addEventListener("click", () => definirModoCadastro(!modoCadastro));

async function cadastrar() {
  if ($("#login-senha").value !== $("#login-senha2").value) {
    $("#login-erro").textContent = "As senhas não conferem.";
    return;
  }
  $("#login-erro").textContent = "Criando conta…";
  const email = $("#login-email").value.trim();
  const { data, error } = await estado.sb.auth.signUp({
    email,
    password: $("#login-senha").value,
    // argos_solicitar_acesso lê o nome de raw_user_meta_data ->> 'nome'.
    options: { data: { nome: $("#login-nome").value.trim() }, emailRedirectTo: location.origin },
  });
  if (error) {
    $("#login-erro").textContent = /already registered/i.test(error.message)
      ? "Esse email já tem conta. Use “Entrar”."
      : error.message;
    return;
  }
  $("#login-senha").value = "";
  $("#login-senha2").value = "";
  // Com confirmação de email ligada no Supabase, o signUp não abre sessão; o
  // pedido de acesso é criado no primeiro login, depois de confirmar.
  // Sem identities = email já cadastrado (o Supabase não revela isso como erro).
  if (!data.session) {
    definirModoCadastro(false);
    $("#login-erro").textContent = data.user?.identities?.length === 0
      ? "Esse email já tem conta. Entre com sua senha."
      : `Enviamos um link de confirmação para ${email}. Confirme e depois entre aqui.`;
    return;
  }
  definirModoCadastro(false);
  await verificarAcesso();
}

$("#form-login").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (modoCadastro) { await cadastrar(); return; }
  $("#login-erro").textContent = "Entrando…";
  const { error } = await estado.sb.auth.signInWithPassword({
    email: $("#login-email").value.trim(),
    password: $("#login-senha").value,
  });
  if (error) {
    $("#login-erro").textContent = error.message === "Invalid login credentials" ? "Email ou senha inválidos." : error.message;
    return;
  }
  $("#login-senha").value = "";
  $("#login-erro").textContent = "";
  await verificarAcesso();
});

$("#btn-verificar").addEventListener("click", () => verificarAcesso());
$("#btn-sair-espera").addEventListener("click", () => sair());

// ------------------------------------------------------------------
// RENDERIZAÇÃO
// ------------------------------------------------------------------
function renderizar() {
  if (!estado.acesso || $("#tela-painel").hidden) return;
  const agora = new Date();
  const snap = estado.snapshot;
  const campo = enriquecer(snap?.campo?.registros, agora);
  const programaveis = enriquecer(snap?.programaveis?.registros, agora);
  const finalizadas = enriquecerFinalizadas(snap?.campo?.finalizadas);

  renderStatus(snap, agora);
  renderExtracao(snap, campo, agora);
  renderAvisos(snap, agora);
  renderKpis(snap, campo, programaveis, finalizadas, agora);
  renderAbas(campo, programaveis, finalizadas, agora);
  renderFiltros(campo, finalizadas);
  if (estado.aba === "acessos" && !ehAdmin()) estado.aba = "campo";
  renderTabela(campo, programaveis, finalizadas, agora);
  $("#usuario-atual").textContent = estado.acesso?.email || "";

  $("#rodape-info").textContent = snap?.gerado_em
    ? `Snapshot gerado às ${fmtHora.format(data(snap.gerado_em))} · atualiza a cada 60s · cores recalculadas a cada 30s`
    : "";
}

function descreverStatus(st) {
  const e = st?.estado || "sem dados";
  if (e === "ok") return { cls: "ok", txt: "ok" };
  if (e === "fora_do_horario") return { cls: "pausa", txt: "fora do horário" };
  if (e.startsWith("erro")) return { cls: "erro", txt: "erro" };
  return { cls: "ocupado", txt: e.replaceAll("_", " ") };
}

function renderStatus(snap, agora) {
  const pills = [];
  for (const [chave, nome] of [["programaveis", "Programáveis"], ["campo", "Em campo"]]) {
    const st = descreverStatus(snap?.status?.[chave]);
    const atualizado = data(snap?.[chave]?.atualizado_em);
    const extra = atualizado ? ` · ${fmtHora.format(atualizado)}` : "";
    pills.push(`<span class="pill" title="${esc(snap?.status?.[chave]?.estado || "")}"><span class="ponto ${st.cls}"></span><b>${nome}</b> ${esc(st.txt)}${esc(extra)}</span>`);
  }
  $("#status").innerHTML = pills.join("");
}

function renderExtracao(snap, campo, agora) {
  const el = $("#extracao");
  if (!snap) { el.hidden = true; return; }
  el.hidden = false;
  const ultima = data(snap.campo?.atualizado_em);
  const proxima = data(snap.campo?.proxima_extracao);
  const st = descreverStatus(snap.status?.campo);

  let proximaTxt, proximaDet;
  if (st.cls === "pausa" || !dentroDaJanela(agora, snap.janela)) {
    proximaTxt = "fora do horário";
    proximaDet = snap.janela ? `retoma às ${snap.janela.inicio}h` : "";
  } else if (!proxima) {
    proximaTxt = "—";
    proximaDet = "";
  } else if (proxima <= agora) {
    proximaTxt = diaHora(proxima);
    proximaDet = st.cls === "ocupado" ? "extraindo agora…" : "a qualquer momento";
  } else {
    proximaTxt = diaHora(proxima);
    proximaDet = `em ${duracao((proxima - agora) / 60000)}`;
  }

  el.innerHTML = `
    <div class="extracao-item">
      <span class="extracao-rotulo">Última extração</span>
      <span class="extracao-valor num">${esc(diaHora(ultima))}</span>
      <span class="extracao-detalhe">${esc(ultima ? haQuanto(ultima, agora) : "ainda não houve extração")}</span>
    </div>
    <div class="extracao-item">
      <span class="extracao-rotulo">Próxima extração</span>
      <span class="extracao-valor num">${esc(proximaTxt)}</span>
      <span class="extracao-detalhe">${esc(proximaDet)}</span>
    </div>
    ${itemProximoVencimento(campo, agora)}`;
}

// Contagem regressiva da próxima ordem em campo ainda no prazo, na cor da
// urgência dela. atualizarContadores() anda o relógio a cada segundo.
function itemProximoVencimento(campo, agora) {
  const proxima = campo.filter((r) => r._min > 0).sort((a, b) => a._min - b._min)[0];
  const cor = proxima ? `style="--cor: var(--${proxima._urg})"` : "";
  const valor = proxima ? `<span data-contagem-ate="${proxima._venc.getTime()}">${cronometro(proxima._venc - agora)}</span>` : "—";
  const detalhe = proxima
    ? `vence ${diaHora(proxima._venc)} · TdC ${proxima.tdc} · ${proxima.equipe || "sem equipe"}`
    : "nenhuma ordem no prazo";
  return `
    <div class="extracao-item extracao-vencimento" ${cor}>
      <span class="extracao-rotulo">Próximo vencimento em</span>
      <span class="extracao-valor num">${valor}</span>
      <span class="extracao-detalhe" title="${esc(detalhe)}">${esc(detalhe)}</span>
    </div>`;
}

function renderAvisos(snap, agora) {
  const avisos = [];
  if (estado.ultimoErro) avisos.push({ erro: true, txt: `Não foi possível atualizar: ${estado.ultimoErro}` });
  if (snap) {
    const naJanela = dentroDaJanela(agora, snap.janela);
    const gerado = data(snap.gerado_em);
    if (naJanela && gerado && agora - gerado > 10 * 60000) {
      avisos.push({ erro: true, txt: `O bot não publica nada ${haQuanto(gerado, agora)} — verifique o container na VPS.` });
    }
    const intervaloCampo = snap.config?.intervalo_campo_min || 30;
    const campoEm = data(snap.campo?.atualizado_em);
    if (naJanela && (!campoEm || agora - campoEm > (intervaloCampo + 15) * 60000)) {
      avisos.push({ txt: `Dados de campo desatualizados (última exportação ${haQuanto(campoEm, agora)}). As cores usam os dados mais recentes disponíveis.` });
    }
    const progEm = data(snap.programaveis?.atualizado_em);
    if (naJanela && (!progEm || agora - progEm > 5 * 60000)) {
      avisos.push({ txt: `Programáveis desatualizadas (última checagem ${haQuanto(progEm, agora)}).` });
    }
    for (const [chave, nome] of [["programaveis", "Programáveis"], ["campo", "Em campo"]]) {
      const e = snap.status?.[chave]?.estado || "";
      if (e.startsWith("erro")) avisos.push({ erro: true, txt: `${nome}: ${e.slice(0, 240)}` });
    }
  }
  $("#avisos").innerHTML = avisos.map((a) => `<div class="aviso${a.erro ? " erro" : ""}">${esc(a.txt)}</div>`).join("");
}

function kpi({ rotulo, valor, detalhe = "", cor = null, acao = null }) {
  const tag = acao ? "button" : "div";
  const estilo = cor ? `style="--cor: var(--${cor}); --cor-valor: var(--${cor})"` : "";
  const zero = valor === 0 ? " zero" : "";
  const dataAcao = acao ? `data-acao="${esc(acao)}"` : "";
  return `<${tag} class="kpi${zero}" ${estilo} ${dataAcao}>
    <div class="rotulo">${esc(rotulo)}</div>
    <div class="valor">${esc(valor)}</div>
    <div class="detalhe">${detalhe}</div>
  </${tag}>`;
}

function renderKpis(snap, campo, programaveis, finalizadas, agora) {
  const conta = (f) => campo.filter((r) => f(r._min)).length;
  const vencidas = conta(FILTRO_URGENCIA.vencida);
  const ate30 = conta(FILTRO_URGENCIA.critico);
  const ate60 = conta(FILTRO_URGENCIA.alerta);
  const hoje = campo.filter((r) => r._min > 0 && mesmoDia(r._venc, agora)).length;
  const novasProg = programaveis.filter((r) => {
    const d = data(r.primeiro_visto_em);
    return d && agora - d <= 60 * 60000;
  }).length;
  const totalProg = snap?.programaveis?.total ?? programaveis.length;
  const finHoje = finalizadas.filter((r) => finalizadaHoje(r, agora));
  const finNoPrazo = finHoje.filter((r) => r.no_prazo === true).length;
  const finForaPrazo = finHoje.filter((r) => r.no_prazo === false).length;

  $("#kpis").innerHTML = [
    kpi({ rotulo: "Em campo", valor: campo.length, detalhe: `exportado ${haQuanto(data(snap?.campo?.atualizado_em), agora)}`, acao: "campo:" }),
    kpi({ rotulo: "Vencidas", valor: vencidas, detalhe: "em aberto no relatório", cor: vencidas ? "vencida" : null, acao: "campo:vencida" }),
    kpi({ rotulo: "Vencem ≤ 30 min", valor: ate30, detalhe: "prioridade máxima", cor: ate30 ? "critico" : null, acao: "campo:critico" }),
    kpi({ rotulo: "Vencem ≤ 1h", valor: ate60, detalhe: "inclui as de ≤ 30 min", cor: ate60 ? "alerta" : null, acao: "campo:alerta" }),
    kpi({ rotulo: "Vencem hoje", valor: hoje, detalhe: "ainda no prazo" }),
    kpi({
      rotulo: "Finalizadas hoje",
      valor: finHoje.length,
      detalhe: finHoje.length ? `${finNoPrazo} no prazo · ${finForaPrazo} fora do prazo` : "nenhuma ainda",
      cor: finHoje.length ? "ok" : null,
      acao: "finalizadas:",
    }),
    kpi({ rotulo: "Programáveis", valor: totalProg, detalhe: novasProg ? `${novasProg} nova(s) em 1h` : "aguardando designação", cor: totalProg ? "atencao" : null, acao: "programaveis:" }),
  ].join("");
}

function renderAbas(campo, programaveis, finalizadas, agora) {
  const finHoje = finalizadas.filter((r) => finalizadaHoje(r, agora));
  $("#cont-campo").textContent = campo.length;
  $("#cont-programaveis").textContent = programaveis.length;
  $("#cont-finalizadas").textContent = finHoje.length;
  $("#cont-equipes").textContent = new Set([...campo, ...finHoje].map((r) => r.equipe || "")).size;
  $("#aba-acessos").hidden = !ehAdmin();
  const pendentes = estado.acessos.filter((a) => a.status === "pendente").length;
  $("#cont-acessos").textContent = pendentes;
  $("#cont-acessos").style.color = pendentes ? "var(--alerta)" : "";
  for (const b of $$(".aba")) b.setAttribute("aria-selected", String(b.dataset.aba === estado.aba));
}

function preencherSelect(sel, valores, atual, rotuloTodos) {
  const opcoes = [`<option value="">${rotuloTodos}</option>`]
    .concat(valores.map((v) => `<option value="${esc(v)}"${v === atual ? " selected" : ""}>${esc(v || "(sem)")}</option>`));
  const html = opcoes.join("");
  if (sel.dataset.html !== html) { sel.innerHTML = html; sel.dataset.html = html; }
  sel.value = atual;
}

function renderFiltros(campo, finalizadas) {
  $("#filtros").hidden = estado.aba === "equipes" || estado.aba === "acessos";
  const comEquipe = ABAS_COM_EQUIPE.has(estado.aba);
  const ehFinalizadas = estado.aba === "finalizadas";
  $("#filtro-equipe").hidden = !comEquipe;
  $("#filtro-bairro").hidden = !comEquipe;
  $("#filtro-urgencia").hidden = ehFinalizadas;
  $("#filtro-periodo").hidden = !ehFinalizadas;
  const base = ehFinalizadas ? finalizadas : campo;
  const unicos = (campo_) => [...new Set(base.map((r) => r[campo_] || ""))].sort((a, b) => a.localeCompare(b, "pt-BR"));
  preencherSelect($("#filtro-equipe"), unicos("equipe"), estado.equipe, "Todas as equipes");
  preencherSelect($("#filtro-bairro"), unicos("bairro"), estado.bairro, "Todos os bairros");
  for (const c of $$("#filtro-urgencia .chip")) c.setAttribute("aria-pressed", String(c.dataset.urg === estado.urg));
  for (const c of $$("#filtro-periodo .chip")) c.setAttribute("aria-pressed", String(c.dataset.periodo === estado.periodo));
}

// ---------- colunas ----------
function textoSituacaoFinalizada(r) {
  if (r.no_prazo === true) return r._folga != null && r._folga >= 1 ? `no prazo · ${duracao(r._folga)} antes` : "no prazo";
  if (r.no_prazo === false) return `fora do prazo · ${duracao(r._folga ?? 0)} depois`;
  return "sem prazo";
}
const celRestante = (r) => `<span class="badge">${esc(textoRestante(r._min))}</span>`;
const COLUNAS = {
  campo: [
    { id: "restante", rotulo: "Tempo restante", valor: (r) => r._min, html: celRestante },
    { id: "vencimento", rotulo: "Vencimento", valor: (r) => r._min, html: (r) => `<span class="num">${esc(diaHora(r._venc))}</span>` },
    { id: "ordem", rotulo: "Ordem", valor: (r) => r.ordem, html: (r) => `<span class="num">${esc(r.ordem)}</span>` },
    { id: "tdc", rotulo: "TdC", valor: (r) => r.tdc, html: (r) => `<span class="num">${esc(r.tdc)}</span>` },
    { id: "cliente", rotulo: "Cliente", valor: (r) => r.cliente, html: (r) => `<span class="num">${esc(r.cliente)}</span>${r.nome_cliente ? `<div class="secundario">${esc(r.nome_cliente)}</div>` : ""}` },
    { id: "equipe", rotulo: "Equipe", valor: (r) => r.equipe, html: (r) => r.equipe ? esc(r.equipe) : `<span class="fraco">sem equipe</span>` },
    { id: "bairro", rotulo: "Bairro", valor: (r) => r.bairro, html: (r) => `${esc(r.bairro)}${r.endereco ? `<div class="secundario celula-endereco" title="${esc(r.endereco)}">${esc(r.endereco)}</div>` : ""}`, cheio: true },
    { id: "tipo", rotulo: "Tipo", valor: (r) => r.tipo, html: (r) => esc(r.tipo), cheio: true },
  ],
  programaveis: [
    { id: "restante", rotulo: "Tempo restante", valor: (r) => r._min, html: celRestante },
    { id: "vencimento", rotulo: "Vencimento", valor: (r) => r._min, html: (r) => `<span class="num">${esc(diaHora(r._venc))}</span>` },
    { id: "tdc", rotulo: "TdC", valor: (r) => r.tdc, html: (r, agora) => {
      const visto = data(r.primeiro_visto_em);
      const novo = visto && agora - visto <= 30 * 60000 ? `<span class="novo">NOVA</span>` : "";
      return `<span class="num">${esc(r.tdc)}</span>${novo}`;
    } },
    { id: "ordem", rotulo: "Ordem", valor: (r) => r.ordem, html: (r) => `<span class="num">${esc(r.ordem)}</span>` },
    { id: "cliente", rotulo: "Cliente", valor: (r) => r.cliente, html: (r) => `<span class="num">${esc(r.cliente)}</span>` },
    { id: "endereco", rotulo: "Endereço", valor: (r) => r.endereco, html: (r) => `<div class="celula-endereco" title="${esc(r.endereco)}">${esc(r.endereco)}</div>`, cheio: true },
    { id: "tipo", rotulo: "Tipo", valor: (r) => r.tipo, html: (r) => `${esc(r.tipo)}${r.atividade ? `<div class="secundario">${esc(r.atividade)}</div>` : ""}`, cheio: true },
    { id: "entrou", rotulo: "Entrou", valor: (r) => r.primeiro_visto_em, html: (r, agora) => `<span class="secundario">${esc(haQuanto(data(r.primeiro_visto_em), agora))}</span>` },
  ],
  finalizadas: [
    { id: "finalizada", rotulo: "Finalizada em", valor: (r) => r.finalizada_em, html: (r) => `<span class="num">${esc(diaHora(r._fim))}</span>` },
    { id: "situacao", rotulo: "Situação", valor: (r) => r._folga, html: (r) => `<span class="badge">${esc(textoSituacaoFinalizada(r))}</span>` },
    { id: "vencimento", rotulo: "Vencimento", valor: (r) => r.vencimento, html: (r) => `<span class="num">${esc(diaHora(r._venc))}</span>` },
    { id: "ordem", rotulo: "Ordem", valor: (r) => r.ordem, html: (r) => `<span class="num">${esc(r.ordem)}</span>` },
    { id: "tdc", rotulo: "TdC", valor: (r) => r.tdc, html: (r) => `<span class="num">${esc(r.tdc)}</span>` },
    { id: "cliente", rotulo: "Cliente", valor: (r) => r.cliente, html: (r) => `<span class="num">${esc(r.cliente)}</span>${r.nome_cliente ? `<div class="secundario">${esc(r.nome_cliente)}</div>` : ""}` },
    { id: "equipe", rotulo: "Equipe", valor: (r) => r.equipe, html: (r) => esc(r.equipe) },
    { id: "bairro", rotulo: "Bairro", valor: (r) => r.bairro, html: (r) => `${esc(r.bairro)}${r.endereco ? `<div class="secundario celula-endereco" title="${esc(r.endereco)}">${esc(r.endereco)}</div>` : ""}`, cheio: true },
    { id: "tipo", rotulo: "Tipo", valor: (r) => r.tipo, html: (r) => esc(r.tipo), cheio: true },
    { id: "resultado", rotulo: "Resultado", valor: (r) => r.resultado, html: (r) => `${esc(r.resultado || "—")}${r.causa ? `<div class="secundario">${esc(r.causa)}</div>` : ""}`, cheio: true },
  ],
  equipes: [
    { id: "equipe", rotulo: "Equipe", valor: (r) => r.equipe, html: (r) => `<button class="link-equipe" data-equipe="${esc(r.equipe)}">${esc(r.equipe || "(sem equipe)")}</button>` },
    { id: "total", rotulo: "Em campo", valor: (r) => r.total, html: (r) => `<span class="num">${r.total}</span>` },
    { id: "vencidas", rotulo: "Vencidas", valor: (r) => r.vencidas, html: (r) => `<span class="num" style="color:${r.vencidas ? "var(--vencida)" : "var(--texto-3)"}">${r.vencidas}</span>` },
    { id: "finalizadas", rotulo: "Finalizadas hoje", valor: (r) => r.finalizadas, html: (r) => `<span class="num" style="color:${r.finalizadas ? "var(--ok)" : "var(--texto-3)"}">${r.finalizadas}</span>` },
    { id: "ate60", rotulo: "Vencem ≤ 1h", valor: (r) => r.ate60, html: (r) => `<span class="num" style="color:${r.ate60 ? "var(--alerta)" : "var(--texto-3)"}">${r.ate60}</span>` },
    { id: "restante", rotulo: "Próximo vencimento", valor: (r) => r._min, html: (r) => r._min == null ? `<span class="fraco">—</span>` : celRestante(r) },
    { id: "distribuicao", rotulo: "Distribuição", valor: null, html: (r) => {
      const seg = ["vencida", "critico", "alerta", "atencao", "ok", "sem"]
        .filter((u) => r.dist[u])
        .map((u) => `<span style="width:${(r.dist[u] / (r.total || 1)) * 100}%;background:var(--${u})" title="${u}: ${r.dist[u]}"></span>`)
        .join("");
      return `<div class="barra">${seg}</div>`;
    }, cheio: true },
  ],
};

const ORDEM_STATUS = { pendente: 0, aprovado: 1, recusado: 2 };
COLUNAS.acessos = [
  { id: "nome", rotulo: "Usuário", valor: (a) => a.nome || a.email, html: (a) =>
    `${esc(a.nome || a.email)}${a.papel === "admin" ? ` <span class="tag admin">admin</span>` : ""}${a.nome ? `<div class="secundario">${esc(a.email)}</div>` : ""}` },
  { id: "status", rotulo: "Status", valor: (a) => ORDEM_STATUS[a.status], html: (a) => `<span class="tag ${esc(a.status)}">${esc(a.status)}</span>` },
  { id: "solicitado", rotulo: "Pediu acesso", valor: (a) => a.solicitado_em, html: (a, agora) => `<span class="secundario">${esc(haQuanto(data(a.solicitado_em), agora))}</span>` },
  { id: "ultimo", rotulo: "Último acesso", valor: (a) => a.ultimo_acesso_em, html: (a, agora) => `<span class="secundario">${esc(a.ultimo_acesso_em ? haQuanto(data(a.ultimo_acesso_em), agora) : "nunca")}</span>` },
  { id: "acoes", rotulo: "Ações", valor: null, cheio: true, html: (a) => {
    const id = esc(a.user_id);
    const eu = a.user_id === estado.acesso?.user_id;
    const b = [];
    if (a.status !== "aprovado") b.push(`<button class="btn-mini aprovar" data-fn="argos_definir_status" data-alvo="${id}" data-valor="aprovado">Aprovar</button>`);
    if (a.status === "pendente") b.push(`<button class="btn-mini recusar" data-fn="argos_definir_status" data-alvo="${id}" data-valor="recusado">Recusar</button>`);
    if (a.status === "aprovado" && !eu) b.push(`<button class="btn-mini recusar" data-fn="argos_definir_status" data-alvo="${id}" data-valor="recusado" data-confirmar="Revogar o acesso de ${esc(a.email)}?">Revogar</button>`);
    if (a.status === "aprovado" && a.papel !== "admin") b.push(`<button class="btn-mini" data-fn="argos_definir_papel" data-alvo="${id}" data-valor="admin" data-confirmar="Tornar ${esc(a.email)} administrador?">Tornar admin</button>`);
    if (a.papel === "admin") b.push(`<button class="btn-mini" data-fn="argos_definir_papel" data-alvo="${id}" data-valor="usuario" data-confirmar="${eu ? "Deixar de ser administrador? Você perde o acesso a esta aba." : `Remover ${esc(a.email)} dos administradores?`}">Remover admin</button>`);
    return `<div class="acoes-linha">${b.join("")}</div>`;
  } },
];

function agruparPorEquipe(campo, finalizadasHoje = []) {
  const grupos = new Map();
  const grupo = (k) => {
    if (!grupos.has(k)) grupos.set(k, { equipe: k, total: 0, vencidas: 0, ate60: 0, finalizadas: 0, _min: null, dist: {} });
    return grupos.get(k);
  };
  for (const r of finalizadasHoje) grupo(r.equipe || "").finalizadas++;
  for (const r of campo) {
    const g = grupo(r.equipe || "");
    g.total++;
    if (FILTRO_URGENCIA.vencida(r._min)) g.vencidas++;
    if (FILTRO_URGENCIA.alerta(r._min)) g.ate60++;
    g.dist[r._urg] = (g.dist[r._urg] || 0) + 1;
    if (r._min != null && r._min > 0 && (g._min == null || r._min < g._min)) g._min = r._min;
  }
  // Equipe com vencida em aberto fica vermelha mesmo que a próxima ainda esteja no prazo.
  return [...grupos.values()].map((g) => ({ ...g, _urg: g.vencidas ? "vencida" : classificar(g._min) }));
}

function filtrar(linhas, aba = estado.aba) {
  const termo = estado.texto.trim().toLowerCase();
  const agora = new Date();
  return linhas.filter((r) => {
    if (ABAS_COM_EQUIPE.has(aba)) {
      if (estado.equipe && (r.equipe || "") !== estado.equipe) return false;
      if (estado.bairro && (r.bairro || "") !== estado.bairro) return false;
    }
    if (aba === "finalizadas") {
      if (estado.periodo === "hoje" && !finalizadaHoje(r, agora)) return false;
    } else if (estado.urg && !FILTRO_URGENCIA[estado.urg](r._min)) return false;
    if (termo) {
      const alvo = [r.ordem, r.tdc, r.cliente, r.equipe, r.bairro, r.tipo, r.endereco, r.nome_cliente, r.resultado].join(" ").toLowerCase();
      if (!alvo.includes(termo)) return false;
    }
    return true;
  });
}

function ordenar(linhas, colunas, aba = estado.aba) {
  const { col, dir } = estado.ordem[aba];
  const def = colunas.find((c) => c.id === col) || colunas[0];
  return [...linhas].sort((a, b) => {
    const va = def.valor(a);
    const vb = def.valor(b);
    const nulaA = va == null || va === "";
    const nulaB = vb == null || vb === "";
    if (nulaA || nulaB) return nulaA === nulaB ? 0 : nulaA ? 1 : -1; // vazios sempre no fim
    if (typeof va === "number" && typeof vb === "number") return (va - vb) * dir;
    return String(va).localeCompare(String(vb), "pt-BR", { numeric: true }) * dir;
  });
}

// Linhas de uma aba já filtradas e ordenadas, como aparecem na tabela.
function linhasDaAba(campo, programaveis, finalizadas, aba = estado.aba) {
  let linhas;
  if (aba === "campo") linhas = filtrar(campo, aba);
  else if (aba === "programaveis") linhas = filtrar(programaveis, aba);
  else if (aba === "finalizadas") linhas = filtrar(finalizadas, aba);
  else if (aba === "acessos") linhas = estado.acessos.map((a) => ({ ...a, _urg: a.status === "pendente" ? "alerta" : a.status === "recusado" ? "sem" : "ok" }));
  else {
    const agora = new Date();
    linhas = agruparPorEquipe(campo, finalizadas.filter((r) => finalizadaHoje(r, agora)));
  }
  return ordenar(linhas, COLUNAS[aba], aba);
}

function renderTabela(campo, programaveis, finalizadas, agora) {
  const colunas = COLUNAS[estado.aba];
  const linhas = linhasDaAba(campo, programaveis, finalizadas);

  const { col, dir } = estado.ordem[estado.aba];
  $("#tabela-cabecalho").innerHTML = `<tr>${colunas.map((c) => {
    if (!c.valor) return `<th>${esc(c.rotulo)}</th>`;
    const ativo = c.id === col;
    const seta = ativo ? (dir > 0 ? "▲" : "▼") : "↕";
    return `<th data-col="${c.id}"${ativo ? ` aria-sort="${dir > 0 ? "ascending" : "descending"}"` : ""}>${esc(c.rotulo)}<span class="seta">${seta}</span></th>`;
  }).join("")}</tr>`;

  $("#tabela-corpo").innerHTML = linhas.map((r) =>
    `<tr class="u-${r._urg}">${colunas.map((c) =>
      `<td data-label="${esc(c.rotulo)}"${c.cheio ? ' class="cheio"' : ""}>${c.html(r, agora)}</td>`
    ).join("")}</tr>`
  ).join("");

  const vazio = $("#vazio");
  vazio.hidden = linhas.length > 0;
  $("#tabela").hidden = linhas.length === 0;
  if (!linhas.length) {
    const base = { programaveis, finalizadas }[estado.aba] || campo;
    const temDados = estado.aba === "finalizadas" && estado.periodo === "hoje"
      ? finalizadas.some((r) => finalizadaHoje(r, agora))
      : base.length > 0;
    const semDados = {
      programaveis: "Nenhuma religação programável no momento.",
      finalizadas: estado.periodo === "hoje" ? "Nenhuma ordem finalizada hoje." : "Nenhuma ordem finalizada no período da extração.",
    }[estado.aba] || "Nenhuma religação em campo no relatório.";
    vazio.textContent = estado.aba === "acessos"
      ? "Nenhum pedido de acesso."
      : !estado.snapshot
      ? "Carregando…"
      : temDados ? "Nenhuma ordem com esses filtros." : semDados;
  }

  if (estado.aba === "programaveis" && estado.snapshot) {
    const total = estado.snapshot.programaveis?.total ?? 0;
    if (total > programaveis.length) {
      vazio.hidden = false;
      vazio.textContent = `O eOrder informa ${total} programáveis; exibindo as ${programaveis.length} da primeira página.`;
    }
  }
}

// ------------------------------------------------------------------
// RELATÓRIO (XLSX)
// ------------------------------------------------------------------
// Uma planilha formatada com três abas (Em campo, Programáveis e Finalizadas), cada uma
// com as ordens como aparecem no painel (filtros e ordenação daquela aba).
// A ExcelJS só é baixada no primeiro clique.
const URL_EXCELJS = "https://cdn.jsdelivr.net/npm/exceljs@4.4.0/dist/exceljs.min.js";
const fmtCompleto = new Intl.DateTimeFormat("pt-BR", { timeZone: TZ, day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const partesData = (d) => Object.fromEntries(fmtCompleto.formatToParts(d).map((p) => [p.type, p.value]));
const NOME_URGENCIA = {
  vencida: "Vencida", critico: "Vence em até 30 min", alerta: "Vence em até 1h",
  atencao: "Vence em até 2h", ok: "No prazo", sem: "Sem prazo",
};
// Mesmas cores do tema claro do painel (styles.css): [texto, fundo].
const COR = {
  texto: "18202B", texto2: "5B6573", borda: "DFE2E8", cabecalho: "0F1720", marca: "E8B64C",
  vencida: ["D63A3A", "FDEAEA"], critico: ["E5731F", "FDF0E5"], alerta: ["A27C05", "FCF6DC"],
  atencao: ["2F6FD6", "E8F0FC"], ok: ["2F9A5D", "E5F5EC"], sem: ["8A93A0", "ECEEF2"],
};
const argb = (rgb) => ({ argb: `FF${rgb}` });
const preenchimento = (rgb) => ({ type: "pattern", pattern: "solid", fgColor: argb(rgb) });

// A ExcelJS grava datas em UTC; passar o relógio de Brasília "como UTC"
// deixa a célula no horário certo, independente do fuso do computador.
function dataBrasilia(d) {
  if (!d) return null;
  const p = partesData(d);
  return new Date(Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute));
}

// [rótulo, valor, tipo] — "data" formata como dd/mm/aaaa hh:mm e
// "urgencia" pinta a célula com a cor da situação.
const COLUNAS_RELATORIO = {
  campo: [
    ["Ordem", (r) => r.ordem],
    ["TdC", (r) => r.tdc],
    ["Cliente", (r) => r.cliente],
    ["Nome do cliente", (r) => r.nome_cliente],
    ["Equipe", (r) => r.equipe],
    ["Bairro", (r) => r.bairro],
    ["Endereço", (r) => r.endereco],
    ["Tipo", (r) => r.tipo],
    ["Vencimento", (r) => dataBrasilia(r._venc), "data"],
    ["Tempo restante", (r) => textoRestante(r._min), "urgencia"],
    ["Situação", (r) => NOME_URGENCIA[r._urg], "urgencia"],
  ],
  programaveis: [
    ["TdC", (r) => r.tdc],
    ["Ordem", (r) => r.ordem],
    ["Cliente", (r) => r.cliente],
    ["Endereço", (r) => r.endereco],
    ["Tipo", (r) => r.tipo],
    ["Atividade", (r) => r.atividade],
    ["Vencimento", (r) => dataBrasilia(r._venc), "data"],
    ["Tempo restante", (r) => textoRestante(r._min), "urgencia"],
    ["Situação", (r) => NOME_URGENCIA[r._urg], "urgencia"],
    ["Entrou no painel", (r) => dataBrasilia(data(r.primeiro_visto_em)), "data"],
  ],
  finalizadas: [
    ["Ordem", (r) => r.ordem],
    ["TdC", (r) => r.tdc],
    ["Cliente", (r) => r.cliente],
    ["Nome do cliente", (r) => r.nome_cliente],
    ["Equipe", (r) => r.equipe],
    ["Bairro", (r) => r.bairro],
    ["Endereço", (r) => r.endereco],
    ["Tipo", (r) => r.tipo],
    ["Vencimento", (r) => dataBrasilia(r._venc), "data"],
    ["Finalizada em", (r) => dataBrasilia(r._fim), "data"],
    ["Situação", (r) => textoSituacaoFinalizada(r), "urgencia"],
    ["Resultado", (r) => r.resultado],
    ["Causa", (r) => r.causa],
  ],
};

let exceljs = null;
function carregarExcelJS() {
  exceljs ??= new Promise((ok, falha) => {
    const s = Object.assign(document.createElement("script"), { src: URL_EXCELJS, onload: () => ok(window.ExcelJS) });
    s.onerror = () => { exceljs = null; s.remove(); falha(new Error("não foi possível carregar o gerador de planilhas")); };
    document.head.append(s);
  });
  return exceljs;
}

function descreverFiltros(aba) {
  const f = [];
  if (estado.texto.trim()) f.push(`busca "${estado.texto.trim()}"`);
  if (aba === "finalizadas") f.push(estado.periodo === "hoje" ? "finalizadas hoje" : "período da extração");
  else if (estado.urg) f.push($(`#filtro-urgencia [data-urg="${estado.urg}"]`)?.textContent || estado.urg);
  if (ABAS_COM_EQUIPE.has(aba) && estado.equipe) f.push(`equipe ${estado.equipe}`);
  if (ABAS_COM_EQUIPE.has(aba) && estado.bairro) f.push(`bairro ${estado.bairro}`);
  return f.length ? `Filtros: ${f.join(", ")}` : "Sem filtros";
}

// Título e resumo nas linhas 1–2, cabeçalho fixo na linha 3, dados a partir da 4.
function abaPlanilha(wb, nome, titulo, resumo, colunas, linhas) {
  const ws = wb.addWorksheet(nome, {
    views: [{ state: "frozen", ySplit: 3, showGridLines: false }],
    pageSetup: { paperSize: 9, orientation: "landscape", fitToPage: true, fitToWidth: 1, fitToHeight: 0 },
  });
  const n = colunas.length;
  const valores = linhas.map((r) => colunas.map(([, valor]) => valor(r) ?? ""));

  ws.mergeCells(1, 1, 1, n);
  Object.assign(ws.getCell(1, 1), { value: titulo, font: { bold: true, size: 15, color: argb(COR.texto) } });
  ws.getRow(1).height = 26;
  ws.mergeCells(2, 1, 2, n);
  Object.assign(ws.getCell(2, 1), { value: resumo, font: { size: 10, color: argb(COR.texto2) } });
  ws.getRow(2).height = 18;

  const cab = ws.getRow(3);
  cab.values = colunas.map(([rotulo]) => rotulo);
  cab.height = 22;
  cab.eachCell((c) => {
    c.font = { bold: true, color: argb("FFFFFF") };
    c.fill = preenchimento(COR.cabecalho);
    c.alignment = { vertical: "middle" };
    c.border = { bottom: { style: "medium", color: argb(COR.marca) } };
  });

  linhas.forEach((r, i) => {
    const [corForte, corFundo] = COR[r._urg] || COR.sem;
    ws.addRow(valores[i]).eachCell({ includeEmpty: true }, (c, col) => {
      const tipo = colunas[col - 1][2];
      c.border = { bottom: { style: "thin", color: argb(COR.borda) } };
      c.alignment = { vertical: "middle" };
      c.font = { color: argb(COR.texto) };
      if (r._urg === "vencida") c.fill = preenchimento(corFundo);  // linha inteira, como no painel
      if (tipo === "data") c.numFmt = "dd/mm/yyyy hh:mm";
      if (tipo === "urgencia") {
        c.font = { bold: true, color: argb(corForte) };
        c.fill = preenchimento(corFundo);
      }
    });
  });

  if (linhas.length) {
    ws.autoFilter = { from: { row: 3, column: 1 }, to: { row: 3 + linhas.length, column: n } };
  } else {
    ws.mergeCells(4, 1, 4, n);
    Object.assign(ws.getCell(4, 1), { value: "Nenhuma ordem.", font: { italic: true, color: argb(COR.texto2) } });
  }

  colunas.forEach(([rotulo, , tipo], i) => {
    const maior = Math.max(rotulo.length + 3, ...valores.map((l) => String(l[i]).length));
    ws.getColumn(i + 1).width = tipo === "data" ? 17 : Math.min(50, maior + 2);
  });
}

function baixarArquivo(blob, nome) {
  const url = URL.createObjectURL(blob);
  const a = Object.assign(document.createElement("a"), { href: url, download: nome });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function exportarRelatorio() {
  if (!estado.snapshot) return;
  const agora = new Date();
  const campo = enriquecer(estado.snapshot.campo?.registros, agora);
  const programaveis = enriquecer(estado.snapshot.programaveis?.registros, agora);
  const finalizadas = enriquecerFinalizadas(estado.snapshot.campo?.finalizadas);

  const btn = $("#btn-exportar");
  btn.disabled = true;
  try {
    const ExcelJS = await carregarExcelJS();
    const wb = new ExcelJS.Workbook();
    wb.creator = "ARGOS";
    wb.created = agora;
    const geradoEm = fmtCompleto.format(agora).replace(",", "");
    const abas = [
      ["campo", "Em campo", "Religas em campo"],
      ["programaveis", "Programáveis", "Religas programáveis"],
      ["finalizadas", "Finalizadas", "Religas finalizadas"],
    ];
    for (const [aba, nome, titulo] of abas) {
      const linhas = linhasDaAba(campo, programaveis, finalizadas, aba);
      const resumo = `Gerado em ${geradoEm} · ${linhas.length} ${linhas.length === 1 ? "ordem" : "ordens"} · ${descreverFiltros(aba)}`;
      abaPlanilha(wb, nome, titulo, resumo, COLUNAS_RELATORIO[aba], linhas);
    }
    const buffer = await wb.xlsx.writeBuffer();
    // ":" não pode em nome de arquivo no Windows, então a hora vai como 14h30.
    const p = partesData(agora);
    baixarArquivo(
      new Blob([buffer], { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" }),
      `Religas - ${p.day}-${p.month}-${p.year} ${p.hour}h${p.minute}.xlsx`,
    );
  } catch (e) {
    alert(`Não foi possível gerar o relatório: ${e.message || e}`);
  } finally {
    btn.disabled = false;
  }
}

// ------------------------------------------------------------------
// EVENTOS
// ------------------------------------------------------------------
function trocarAba(aba) {
  estado.aba = aba;
  salvarPrefs();
  renderizar();
}

$$(".aba").forEach((b) => b.addEventListener("click", () => trocarAba(b.dataset.aba)));

$("#kpis").addEventListener("click", (ev) => {
  const alvo = ev.target.closest("[data-acao]");
  if (!alvo) return;
  const [aba, urg] = alvo.dataset.acao.split(":");
  estado.urg = urg || "";
  if (aba === "finalizadas") estado.periodo = "hoje";
  estado.equipe = "";
  estado.bairro = "";
  trocarAba(aba);
});

$("#tabela-cabecalho").addEventListener("click", (ev) => {
  const th = ev.target.closest("th[data-col]");
  if (!th) return;
  const o = estado.ordem[estado.aba];
  if (o.col === th.dataset.col) o.dir *= -1;
  else { o.col = th.dataset.col; o.dir = ["vencidas", "total", "ate60", "finalizadas", "finalizada"].includes(th.dataset.col) ? -1 : 1; }
  renderizar();
});

$("#tabela-corpo").addEventListener("click", async (ev) => {
  const acao = ev.target.closest("[data-fn]");
  if (acao) {
    if (acao.dataset.confirmar && !confirm(acao.dataset.confirmar)) return;
    acao.disabled = true;
    const campoValor = acao.dataset.fn === "argos_definir_status" ? "p_status" : "p_papel";
    await acaoAcesso(acao.dataset.fn, { p_alvo: acao.dataset.alvo, [campoValor]: acao.dataset.valor });
    return;
  }
  const b = ev.target.closest("[data-equipe]");
  if (!b) return;
  estado.equipe = b.dataset.equipe;
  estado.urg = "";
  trocarAba("campo");
});

$("#filtro-texto").addEventListener("input", (ev) => { estado.texto = ev.target.value; renderizar(); });
$("#filtro-equipe").addEventListener("change", (ev) => { estado.equipe = ev.target.value; renderizar(); });
$("#filtro-bairro").addEventListener("change", (ev) => { estado.bairro = ev.target.value; renderizar(); });
$("#filtro-urgencia").addEventListener("click", (ev) => {
  const c = ev.target.closest(".chip");
  if (!c) return;
  estado.urg = estado.urg === c.dataset.urg ? "" : c.dataset.urg;
  renderizar();
});

$("#filtro-periodo").addEventListener("click", (ev) => {
  const c = ev.target.closest(".chip");
  if (!c) return;
  estado.periodo = c.dataset.periodo;
  renderizar();
});

$("#btn-atualizar").addEventListener("click", () => buscarSnapshot());
$("#btn-exportar").addEventListener("click", exportarRelatorio);
$("#btn-sair").addEventListener("click", () => sair());
$("#aba-acessos").addEventListener("click", () => carregarAcessos().then(renderizar));
$("#btn-tema").addEventListener("click", () => {
  const raiz = document.documentElement;
  const escuroAtual = raiz.dataset.theme
    ? raiz.dataset.theme === "dark"
    : window.matchMedia("(prefers-color-scheme: dark)").matches;
  raiz.dataset.theme = escuroAtual ? "light" : "dark";
  salvarPrefs();
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && estado.acesso && !$("#tela-painel").hidden) buscarSnapshot();
});


// ------------------------------------------------------------------
// NOTIFICAÇÕES PELO PAINEL (Web Push)
// ------------------------------------------------------------------
// O sino inscreve este navegador (service worker sw.js) e grava a inscrição
// no Supabase (argos_push_inscrever). O bot envia para todas as inscrições
// de usuários aprovados. A chave pública VAPID vem no snapshot.
const pushSuportado = () => "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
const ehIOS = () => /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const instaladoNaTela = () => window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
const chavePushServidor = () => estado.snapshot?.config?.push_chave_publica || null;

let registroSW = null;
async function registrarSW() {
  if (!("serviceWorker" in navigator)) return null;
  try { registroSW = await navigator.serviceWorker.register("/sw.js"); } catch { registroSW = null; }
  return registroSW;
}

function base64UrlParaBytes(b64) {
  const bin = atob((b64 + "=".repeat((4 - (b64.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
}
function bytesParaBase64Url(buf) {
  return btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function inscricaoAtual() {
  if (!pushSuportado()) return null;
  const reg = registroSW || await registrarSW();
  return reg ? reg.pushManager.getSubscription() : null;
}

async function salvarInscricao(sub) {
  const j = sub.toJSON();
  const { error } = await estado.sb.rpc("argos_push_inscrever", {
    p_endpoint: j.endpoint, p_p256dh: j.keys.p256dh, p_auth: j.keys.auth, p_user_agent: navigator.userAgent,
  });
  if (error) throw new Error(error.message);
}

async function atualizarEstadoPush() {
  if (!pushSuportado()) estado.push = ehIOS() && !instaladoNaTela() ? "ios_instalar" : "indisponivel";
  else if (Notification.permission === "denied") estado.push = "bloqueado";
  else estado.push = Notification.permission === "granted" && await inscricaoAtual() ? "ativo" : "desativado";
  renderPush();
}

function renderPush() {
  const btn = $("#btn-push");
  const textos = {
    ativo: "Notificações ativadas neste aparelho (clique para desativar)",
    desativado: "Ativar notificações neste aparelho",
    bloqueado: "Notificações bloqueadas no navegador",
    ios_instalar: "Ativar notificações (requer instalar o ARGOS na Tela de Início)",
    indisponivel: "Este navegador não suporta notificações",
  };
  btn.hidden = !estado.push;
  btn.setAttribute("aria-pressed", String(estado.push === "ativo"));
  btn.classList.toggle("bloqueado", estado.push === "bloqueado" || estado.push === "indisponivel");
  btn.title = textos[estado.push] || "";
  btn.setAttribute("aria-label", btn.title);
}

// Depois do login: prende a inscrição existente ao usuário atual e refaz
// se o servidor trocou a chave VAPID (a inscrição antiga deixa de valer).
async function sincronizarPush() {
  try {
    const sub = pushSuportado() && Notification.permission === "granted" ? await inscricaoAtual() : null;
    const chave = chavePushServidor();
    if (sub && chave) {
      const atual = sub.options?.applicationServerKey;
      if (atual && bytesParaBase64Url(atual) !== chave) {
        await sub.unsubscribe();
        await salvarInscricao(await registroSW.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: base64UrlParaBytes(chave) }));
      } else {
        await salvarInscricao(sub);
      }
    }
  } catch { /* tenta de novo no próximo login */ }
  await atualizarEstadoPush();
}

async function ativarPush() {
  const chave = chavePushServidor();
  if (!chave) { alert("As notificações pelo painel ainda não foram configuradas no servidor."); return; }
  // Pedir a permissão logo no clique: o Safari recusa se vier depois de outro await.
  const permissao = await Notification.requestPermission();
  if (permissao !== "granted") { await atualizarEstadoPush(); return; }
  try {
    const reg = registroSW || await registrarSW();
    if (!reg) throw new Error("não foi possível registrar o service worker");
    await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: base64UrlParaBytes(chave) });
    await salvarInscricao(sub);
    reg.showNotification("ARGOS", { body: "Notificações ativadas neste aparelho.", icon: "/icones/icone-192.png" });
  } catch (e) {
    alert(`Não foi possível ativar as notificações: ${e.message || e}`);
  }
  await atualizarEstadoPush();
}

async function desativarPush({ silencioso = false } = {}) {
  try {
    const sub = await inscricaoAtual();
    if (sub) {
      await estado.sb.rpc("argos_push_cancelar", { p_endpoint: sub.endpoint });  // devolve {error}, não lança
      await sub.unsubscribe();
    }
  } catch (e) {
    if (!silencioso) alert(`Não foi possível desativar: ${e.message || e}`);
  }
  if (!silencioso) await atualizarEstadoPush();
}

$("#btn-push").addEventListener("click", async () => {
  switch (estado.push) {
    case "desativado": return ativarPush();
    case "ativo":
      if (confirm("Desativar as notificações do ARGOS neste aparelho?")) await desativarPush();
      return;
    case "bloqueado":
      return alert("As notificações do ARGOS estão bloqueadas neste navegador. Libere nas configurações do site (ícone ao lado do endereço) e clique no sino de novo.");
    case "ios_instalar":
      return alert("No iPhone/iPad, as notificações só funcionam com o ARGOS instalado:\n\n1. Toque em Compartilhar (quadrado com a seta) no Safari.\n2. Escolha \"Adicionar à Tela de Início\".\n3. Abra o ARGOS pelo ícone criado, entre e toque no sino.\n\nRequer iOS 16.4 ou mais recente.");
    default:
      return alert("Este navegador não suporta notificações. Use Chrome, Edge ou Firefox.");
  }
});

// ------------------------------------------------------------------
// INÍCIO
// ------------------------------------------------------------------
// Anda o cronômetro do "Próximo vencimento" a cada segundo sem redesenhar
// o painel inteiro; ao zerar, redesenha para pegar a próxima ordem.
function atualizarContadores() {
  const agora = Date.now();
  for (const el of $$("[data-contagem-ate]")) {
    const restante = Number(el.dataset.contagemAte) - agora;
    if (restante <= 0) { renderizar(); return; }
    el.textContent = cronometro(restante);
  }
}

let timers = [];
function pararTimers() {
  timers.forEach(clearInterval);
  timers = [];
}

function iniciarPainel() {
  mostrarTela("#tela-painel");
  estado.pushSincronizado = false;
  atualizarEstadoPush();
  pararTimers();
  timers = [
    setInterval(() => { if (document.visibilityState === "visible") buscarSnapshot(); }, INTERVALO_BUSCA_MS),
    setInterval(renderizar, INTERVALO_RELOGIO_MS),
    setInterval(atualizarContadores, INTERVALO_CONTADOR_MS),
  ];
  renderizar();
  buscarSnapshot();
}

async function iniciar() {
  if (prefsSalvas.tema) document.documentElement.dataset.theme = prefsSalvas.tema;
  registrarSW();
  try {
    const r = await fetch("/api/config");
    const cfg = await r.json();
    if (!r.ok) throw new Error(cfg.erro || `Erro ${r.status}`);
    // O supabase-js guarda a sessão no localStorage e renova o token sozinho.
    estado.sb = window.supabase.createClient(cfg.supabaseUrl, cfg.supabaseAnonKey, {
      auth: { persistSession: true, autoRefreshToken: true, storageKey: "argos.auth" },
    });
  } catch (e) {
    mostrarLogin(`Painel sem configuração: ${e.message}`);
    $$("#form-login button").forEach((b) => { b.disabled = true; });
    return;
  }
  estado.sb.auth.onAuthStateChange((evento) => {
    if (evento === "SIGNED_OUT" && estado.acesso) {
      estado.acesso = null;
      mostrarLogin();
    }
  });
  await verificarAcesso();
}

iniciar();
