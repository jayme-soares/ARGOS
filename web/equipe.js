"use strict";

// ARGOS — painel das equipes de campo (/equipe). Cada equipe entra com o
// código (NI2...) e a senha e vê só as próprias religas: as em aberto, com
// contagem regressiva, e as finalizadas, no prazo ou fora. O filtro por
// equipe é feito no servidor (/api/snapshot).
//
// Avisos (religa designada, perto de vencer, vencida) chegam por push e
// ficam pendentes em argos_avisos_equipe até a equipe confirmar: enquanto
// houver pendentes, um modal bloqueia o painel.

const CHAVE_PREFS = "argos.equipe.prefs";

const prefsSalvas = (() => {
  try { return JSON.parse(lerStorage(localStorage, CHAVE_PREFS) || "{}"); } catch { return {}; }
})();

Object.assign(estado, {
  aba: "abertas",
  periodo: "hoje",
  avisos: [],        // avisos pendentes da equipe
  cfg: null,         // /api/config
});

function salvarPrefs() {
  gravarStorage(localStorage, CHAVE_PREFS, JSON.stringify({ tema: document.documentElement.dataset.theme || null }));
}

const normalizarCodigo = (c) => String(c || "").replace(/\s+/g, "").toUpperCase();
const emailDaEquipe = (codigo) => `${normalizarCodigo(codigo).toLowerCase()}@${estado.cfg?.dominioEquipes || "equipes.argos.local"}`;

// ------------------------------------------------------------------
// TELAS
// ------------------------------------------------------------------
const TELAS = ["#tela-login", "#tela-espera", "#tela-senha", "#tela-painel"];
function mostrarTela(id) {
  for (const t of TELAS) $(t).hidden = t !== id;
  if (id !== "#tela-painel") $("#modal-avisos").hidden = true;
}

function mostrarLogin(mensagem) {
  pararTimers();
  mostrarTela("#tela-login");
  $("#login-erro").textContent = mensagem || "";
  $("#login-codigo").focus();
}

function mostrarEspera(acesso) {
  pararTimers();
  mostrarTela("#tela-espera");
  const recusado = acesso?.status === "recusado";
  $("#espera-titulo").textContent = recusado ? "Acesso não liberado" : "Aguardando aprovação";
  $("#espera-texto").textContent = recusado
    ? "O acesso desta equipe foi recusado ou revogado pela coordenação. Fale com o administrador do ARGOS."
    : "O acesso da equipe foi registrado. Assim que um administrador aprovar, toque em “Verificar novamente”.";
  $("#espera-equipe").textContent = acesso?.equipe || "";
}

function mostrarTrocaSenha() {
  pararTimers();
  mostrarTela("#tela-senha");
  $("#senha-erro").textContent = "";
  $("#senha-nova").focus();
}

async function sair(mensagem) {
  await desativarPush({ silencioso: true });
  estado.acesso = null;
  estado.snapshot = null;
  estado.avisos = [];
  try { await estado.sb.auth.signOut(); } catch { /* sessão já inválida */ }
  mostrarLogin(mensagem);
}

async function verificarAcesso() {
  const { data: sessao } = await estado.sb.auth.getSession();
  if (!sessao.session) { mostrarLogin(); return; }
  const { data, error } = await estado.sb.rpc("argos_solicitar_acesso");
  if (error) { mostrarLogin(`Não foi possível verificar o acesso: ${error.message}`); return; }
  // Conta da gestão: painel completo.
  if (data?.papel !== "equipe") { location.replace("/"); return; }
  estado.acesso = data;
  if (data.status !== "aprovado") mostrarEspera(data);
  else if (data.trocar_senha) mostrarTrocaSenha();
  else if ($("#tela-painel").hidden) iniciarPainel();
}

// ------------------------------------------------------------------
// LOGIN / CADASTRO / SENHA
// ------------------------------------------------------------------
let modoCadastro = false;
function definirModoCadastro(ativo) {
  modoCadastro = ativo;
  for (const el of $$("[data-so-cadastro]")) {
    el.hidden = !ativo;
    el.querySelector("input").disabled = !ativo;
  }
  $("#login-senha").autocomplete = ativo ? "new-password" : "current-password";
  $("#login-senha").minLength = ativo ? 8 : 0;
  $("#btn-login").textContent = ativo ? "Criar acesso da equipe" : "Entrar";
  $("#btn-alternar-cadastro").textContent = ativo ? "Já tenho acesso: entrar" : "Primeiro acesso? Cadastre a equipe";
  $("#btn-esqueci").hidden = ativo;
  $("#login-dica").textContent = ativo
    ? "Use o código da equipe (começa com NI2) e crie uma senha de pelo menos 8 caracteres. Um administrador precisa aprovar o acesso."
    : "Entre com o código da equipe (começa com NI2) e a senha.";
  $("#login-erro").textContent = "";
}
definirModoCadastro(false);
$("#btn-alternar-cadastro").addEventListener("click", () => definirModoCadastro(!modoCadastro));

async function entrar(codigo, senha) {
  const { error } = await estado.sb.auth.signInWithPassword({ email: emailDaEquipe(codigo), password: senha });
  if (error) {
    $("#login-erro").textContent = error.message === "Invalid login credentials" ? "Código ou senha inválidos." : error.message;
    return false;
  }
  return true;
}

async function cadastrar(codigo, senha) {
  if (senha !== $("#login-senha2").value) { $("#login-erro").textContent = "As senhas não conferem."; return false; }
  $("#login-erro").textContent = "Criando acesso…";
  const r = await fetch("/api/equipe-cadastro", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ codigo, senha }),
  });
  const corpo = await r.json().catch(() => ({}));
  if (!r.ok) { $("#login-erro").textContent = corpo.erro || `Erro ${r.status}`; return false; }
  definirModoCadastro(false);
  return entrar(codigo, senha);
}

$("#form-login").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const codigo = normalizarCodigo($("#login-codigo").value);
  const senha = $("#login-senha").value;
  if (!/^NI2/.test(codigo)) { $("#login-erro").textContent = "O código da equipe começa com NI2."; return; }
  $("#btn-login").disabled = true;
  try {
    if (!modoCadastro) $("#login-erro").textContent = "Entrando…";
    const ok = modoCadastro ? await cadastrar(codigo, senha) : await entrar(codigo, senha);
    if (!ok) return;
    $("#login-senha").value = "";
    $("#login-senha2").value = "";
    $("#login-erro").textContent = "";
    await verificarAcesso();
  } finally {
    $("#btn-login").disabled = false;
  }
});

$("#btn-esqueci").addEventListener("click", async () => {
  const codigo = normalizarCodigo($("#login-codigo").value);
  if (!/^NI2/.test(codigo)) {
    $("#login-erro").textContent = "Preencha o código da equipe e toque em “Esqueci a senha” de novo.";
    $("#login-codigo").focus();
    return;
  }
  const { error } = await estado.sb.rpc("argos_equipe_pedir_reset", { p_codigo: codigo });
  $("#login-erro").textContent = error
    ? `Não foi possível registrar o pedido: ${error.message}`
    : "Pedido registrado. O administrador vai gerar uma senha temporária: fale com a coordenação para recebê-la.";
});

$("#form-senha").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const nova = $("#senha-nova").value;
  if (nova !== $("#senha-nova2").value) { $("#senha-erro").textContent = "As senhas não conferem."; return; }
  $("#senha-erro").textContent = "Salvando…";
  const { error } = await estado.sb.auth.updateUser({ password: nova });
  if (error) { $("#senha-erro").textContent = error.message; return; }
  const r = await estado.sb.rpc("argos_senha_trocada");
  if (r.error) { $("#senha-erro").textContent = r.error.message; return; }
  $("#senha-nova").value = "";
  $("#senha-nova2").value = "";
  await verificarAcesso();
});

$("#btn-verificar").addEventListener("click", () => verificarAcesso());
$("#btn-sair-espera").addEventListener("click", () => sair());
$("#btn-sair-senha").addEventListener("click", () => sair());

// ------------------------------------------------------------------
// DADOS
// ------------------------------------------------------------------
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
    if (r.status === 403) { await verificarAcesso(); return false; }  // revogado ou senha a trocar
    if (!r.ok) throw new Error(corpo.erro || `Erro ${r.status}`);
    estado.snapshot = corpo;
    estado.ultimoErro = null;
    if (!estado.pushSincronizado) { estado.pushSincronizado = true; sincronizarPush(); }
    return true;
  } catch (e) {
    estado.ultimoErro = e.message || String(e);
    return false;
  } finally {
    await carregarAvisos();
    btn.classList.remove("girando");
    renderizar();
  }
}

// Avisos pendentes da equipe (o RLS só devolve os da própria equipe).
async function carregarAvisos() {
  if (!estado.acesso) return;
  const { data, error } = await estado.sb
    .from("argos_avisos_equipe")
    .select("id, tipo, nivel, tdc, ordem, vencimento, titulo, mensagem, criado_em, reenvios")
    .eq("status", "pendente")
    .order("criado_em", { ascending: true });
  if (!error) estado.avisos = data || [];
  renderModal();
}

async function confirmarAvisos() {
  const ids = estado.avisos.map((a) => a.id);
  if (!ids.length) return;
  const btn = $("#btn-confirmar-avisos");
  btn.disabled = true;
  $("#modal-erro").textContent = "";
  try {
    const { error } = await estado.sb.rpc("argos_avisos_confirmar", { p_ids: ids });
    if (error) { $("#modal-erro").textContent = `Não foi possível confirmar: ${error.message}`; return; }
    await carregarAvisos();
  } finally {
    btn.disabled = false;
  }
}

// ------------------------------------------------------------------
// RENDERIZAÇÃO
// ------------------------------------------------------------------
const NOME_TIPO = { designada: "Nova religa", vencer: "Perto de vencer", vencida: "Vencida" };
const COR_TIPO = { designada: "atencao", vencer: "alerta", vencida: "vencida" };

function renderModal() {
  const modal = $("#modal-avisos");
  const n = estado.avisos.length;
  modal.hidden = n === 0 || $("#tela-painel").hidden;
  document.body.classList.toggle("bloqueado", !modal.hidden);
  if (modal.hidden) return;
  const agora = new Date();
  $("#modal-titulo").textContent = n === 1 ? "1 aviso para confirmar" : `${n} avisos para confirmar`;
  const html = estado.avisos.map((a) => `
    <li class="modal-item" style="--cor: var(--${COR_TIPO[a.tipo] || "sem"})">
      <span class="modal-tipo">${esc(NOME_TIPO[a.tipo] || a.tipo)}</span>
      <strong>${esc(a.titulo)}</strong>
      ${a.mensagem ? `<span class="secundario">${esc(a.mensagem)}</span>` : ""}
      <span class="fraco">enviado ${esc(haQuanto(data(a.criado_em), agora))}${a.reenvios ? ` · reenviado ${a.reenvios}×` : ""}</span>
    </li>`).join("");
  if ($("#modal-lista").dataset.html !== html) {
    $("#modal-lista").innerHTML = html;
    $("#modal-lista").dataset.html = html;
  }
  $("#btn-confirmar-avisos").textContent = n === 1 ? "Confirmo que visualizei" : `Confirmo que visualizei os ${n}`;
}

function renderizar() {
  if (!estado.acesso || $("#tela-painel").hidden) return;
  const agora = new Date();
  const snap = estado.snapshot;
  const abertas = enriquecer(snap?.campo?.registros, agora).sort((a, b) => (a._min ?? Infinity) - (b._min ?? Infinity));
  const finalizadas = enriquecerFinalizadas(snap?.campo?.finalizadas);
  const finHoje = finalizadas.filter((r) => finalizadaHoje(r, agora));

  $("#nome-equipe").textContent = `Equipe ${estado.acesso.equipe || ""}`;
  const st = descreverStatus(snap?.status?.campo);
  const atualizado = data(snap?.campo?.atualizado_em);
  $("#status").innerHTML = `<span class="pill"><span class="ponto ${st.cls}"></span><b>Dados</b> ${esc(st.txt)}${atualizado ? ` · ${esc(fmtHora.format(atualizado))}` : ""}</span>`;
  renderExtracao(snap, abertas, agora);
  renderAvisosTopo(snap, agora);
  renderFaixaPush();

  const vencidas = abertas.filter((r) => FILTRO_URGENCIA.vencida(r._min)).length;
  const ate60 = abertas.filter((r) => FILTRO_URGENCIA.alerta(r._min)).length;
  const noPrazo = finHoje.filter((r) => r.no_prazo === true).length;
  const foraPrazo = finHoje.filter((r) => r.no_prazo === false).length;
  $("#kpis").innerHTML = [
    kpi({ rotulo: "Em aberto", valor: abertas.length, detalhe: "designadas para a equipe", acao: "abertas" }),
    kpi({ rotulo: "Vencidas", valor: vencidas, detalhe: "finalize com prioridade", cor: vencidas ? "vencida" : null, acao: "abertas" }),
    kpi({ rotulo: "Vencem ≤ 1h", valor: ate60, detalhe: "inclui as de ≤ 30 min", cor: ate60 ? "alerta" : null, acao: "abertas" }),
    kpi({
      rotulo: "Finalizadas hoje",
      valor: finHoje.length,
      detalhe: finHoje.length ? `${noPrazo} no prazo · ${foraPrazo} fora` : "nenhuma ainda",
      cor: finHoje.length ? (foraPrazo ? "alerta" : "ok") : null,
      acao: "finalizadas",
    }),
  ].join("");

  $("#cont-abertas").textContent = abertas.length;
  $("#cont-finalizadas").textContent = finHoje.length;
  for (const b of $$(".aba")) b.setAttribute("aria-selected", String(b.dataset.aba === estado.aba));
  $("#filtro-periodo-wrap").hidden = estado.aba !== "finalizadas";
  for (const c of $$("#filtro-periodo .chip")) c.setAttribute("aria-pressed", String(c.dataset.periodo === estado.periodo));

  const lista = estado.aba === "abertas"
    ? abertas
    : estado.periodo === "hoje" ? finHoje : finalizadas;
  $("#lista").innerHTML = lista.map((r) => (estado.aba === "abertas" ? cartaoAberta(r) : cartaoFinalizada(r))).join("");
  const vazio = $("#vazio");
  vazio.hidden = lista.length > 0;
  vazio.textContent = !snap
    ? "Carregando…"
    : estado.aba === "abertas"
    ? "Nenhuma religa em aberto para a equipe."
    : estado.periodo === "hoje" ? "Nenhuma religa finalizada hoje." : "Nenhuma religa finalizada nos últimos 7 dias.";

  $("#rodape-info").textContent = snap?.gerado_em
    ? `Dados de ${fmtHora.format(data(snap.gerado_em))} · atualiza a cada 60s`
    : "";
  renderModal();
}

function renderAvisosTopo(snap, agora) {
  const avisos = [];
  if (estado.ultimoErro) avisos.push({ erro: true, txt: `Não foi possível atualizar: ${estado.ultimoErro}` });
  if (snap && dentroDaJanela(agora, snap.janela)) {
    const campoEm = data(snap.campo?.atualizado_em);
    const intervalo = snap.config?.intervalo_campo_min || 30;
    if (!campoEm || agora - campoEm > (intervalo + 15) * 60000) {
      avisos.push({ txt: `Dados desatualizados (última atualização ${haQuanto(campoEm, agora)}). Confira também no eOrder.` });
    }
  }
  $("#avisos").innerHTML = avisos.map((a) => `<div class="aviso${a.erro ? " erro" : ""}">${esc(a.txt)}</div>`).join("");
}

function renderFaixaPush() {
  $("#faixa-push").hidden = !estado.acesso || !estado.push || estado.push === "ativo";
  $("#btn-faixa-push").textContent = estado.push === "desativado" ? "Ativar" : "Como ativar";
}
document.addEventListener("argos:push", () => { if (estado.acesso) renderFaixaPush(); });

function linkMapa(r) {
  const alvo = [r.endereco, r.bairro, "Maricá - RJ"].filter(Boolean).join(", ");
  return r.endereco ? `<a class="eq-mapa" href="https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(alvo)}" target="_blank" rel="noopener">Abrir no mapa</a>` : "";
}

function cartaoAberta(r) {
  const contagem = r._min > 0
    ? `<span class="num eq-contagem" data-contagem-ate="${r._venc.getTime()}">${cronometro(r._venc - new Date())}</span>`
    : "";
  return `
    <article class="eq-card u-${r._urg}">
      <div class="eq-linha">
        <span class="badge">${esc(textoRestante(r._min))}</span>
        ${contagem}
      </div>
      <div class="eq-linha eq-ids">
        <span>TdC <b class="num">${esc(r.tdc)}</b></span>
        <span>Ordem <b class="num">${esc(r.ordem)}</b></span>
      </div>
      <div class="eq-vencimento">Vence <b class="num">${esc(diaHora(r._venc))}</b></div>
      ${r.endereco ? `<div class="eq-endereco">${esc(r.endereco)}</div>` : ""}
      <div class="secundario">${esc([r.bairro, r.tipo].filter(Boolean).join(" · "))}</div>
      ${r.cliente || r.nome_cliente ? `<div class="secundario">Cliente <span class="num">${esc(r.cliente)}</span>${r.nome_cliente ? ` · ${esc(r.nome_cliente)}` : ""}</div>` : ""}
      ${linkMapa(r)}
    </article>`;
}

function cartaoFinalizada(r) {
  return `
    <article class="eq-card u-${r._urg}">
      <div class="eq-linha">
        <span class="badge">${esc(textoSituacaoFinalizada(r))}</span>
        <span class="secundario">finalizada <b class="num">${esc(diaHora(r._fim))}</b></span>
      </div>
      <div class="eq-linha eq-ids">
        <span>TdC <b class="num">${esc(r.tdc)}</b></span>
        <span>Ordem <b class="num">${esc(r.ordem)}</b></span>
      </div>
      <div class="secundario">Vencimento <span class="num">${esc(diaHora(r._venc))}</span></div>
      ${r.endereco ? `<div class="eq-endereco">${esc(r.endereco)}</div>` : ""}
      <div class="secundario">${esc([r.bairro, r.tipo].filter(Boolean).join(" · "))}</div>
      ${r.resultado ? `<div class="secundario">Resultado: ${esc(r.resultado)}${r.causa ? ` · ${esc(r.causa)}` : ""}</div>` : ""}
    </article>`;
}

// ------------------------------------------------------------------
// EVENTOS
// ------------------------------------------------------------------
function trocarAba(aba) {
  estado.aba = aba;
  renderizar();
  window.scrollTo({ top: $("#kpis").offsetTop - 70, behavior: "smooth" });
}
$$(".aba").forEach((b) => b.addEventListener("click", () => trocarAba(b.dataset.aba)));
$("#kpis").addEventListener("click", (ev) => {
  const alvo = ev.target.closest("[data-acao]");
  if (!alvo) return;
  if (alvo.dataset.acao === "finalizadas") estado.periodo = "hoje";
  trocarAba(alvo.dataset.acao);
});
$("#filtro-periodo").addEventListener("click", (ev) => {
  const c = ev.target.closest(".chip");
  if (!c) return;
  estado.periodo = c.dataset.periodo;
  renderizar();
});
$("#btn-confirmar-avisos").addEventListener("click", confirmarAvisos);
$("#btn-faixa-push").addEventListener("click", () => $("#btn-push").click());
$("#btn-atualizar").addEventListener("click", () => buscarSnapshot());
$("#btn-sair").addEventListener("click", () => sair());
$("#btn-tema").addEventListener("click", () => {
  const raiz = document.documentElement;
  const escuroAtual = raiz.dataset.theme
    ? raiz.dataset.theme === "dark"
    : window.matchMedia("(prefers-color-scheme: dark)").matches;
  raiz.dataset.theme = escuroAtual ? "light" : "dark";
  salvarPrefs();
});

// Volta para o painel (ex.: tocou na notificação): busca na hora, para o
// modal de confirmação aparecer sem esperar o próximo ciclo.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && estado.acesso && !$("#tela-painel").hidden) buscarSnapshot();
});

// ------------------------------------------------------------------
// INÍCIO
// ------------------------------------------------------------------
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
    estado.cfg = await criarSupabase();
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
