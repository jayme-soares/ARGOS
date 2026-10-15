"use strict";

// ARGOS — painel de religações. Lê /api/snapshot (publicado pelo bot no
// Upstash) a cada 60s e recalcula a urgência no navegador a cada 30s, para
// as cores e contagens regressivas andarem entre uma leitura e outra.
//
// Login pelo Supabase Auth (usuários da empresa). O acesso ao painel depende
// da aprovação de um admin na tabela argos_acessos — ver supabase/migrations.
//
// Um painel por contrato (SOC e Negociação). Cada usuário vê os contratos
// que o administrador geral liberou (argos_acessos.contratos); quem tem mais
// de um alterna pelo seletor no topo.

const CHAVE_PREFS = "argos.prefs";

// ------------------------------------------------------------------
// ESTADO
// ------------------------------------------------------------------
const prefsSalvas = (() => {
  try { return JSON.parse(lerStorage(localStorage, CHAVE_PREFS) || "{}"); } catch { return {}; }
})();

const NOMES_CONTRATO = { soc: "SOC · Maricá", negociacao: "Negociação" };
const CONTRATOS = Object.keys(NOMES_CONTRATO);

Object.assign(estado, {
  aba: prefsSalvas.aba || "campo",
  contrato: null,    // contrato exibido (ver escolherContrato)
  texto: "",
  equipe: "",
  bairro: "",
  tramite: "",       // Medidor/Poste/Ramal; SEM_TRAMITE = ordens sem essa indicação
  // COs escolhidos por contrato (só Negociação; vazio = todos). Valem para o
  // painel inteiro e ficam salvos no navegador: cada pessoa acompanha os seus.
  cosPorContrato: prefsSalvas.cos || {},
  urg: "",
  periodo: "hoje",   // aba Finalizadas: "hoje" ou "" (toda a janela da extração)
  ordem: {
    campo: { col: "restante", dir: 1 },
    programaveis: { col: "restante", dir: 1 },
    finalizadas: { col: "finalizada", dir: -1 },
    equipes: { col: "vencidas", dir: -1 },
    acessos: { col: "status", dir: 1 },
    avisos: { col: "enviado", dir: -1 },
  },
  statusAviso: "",   // aba Avisos: "", "pendente", "confirmado" ou "dispensado"
  acessos: [],       // todas as linhas (só admins)
  equipesResumo: [], // contas das equipes: última visualização, push, pendências
  avisosEquipe: [],  // avisos às equipes dos últimos 7 dias
  // Os dois acima já filtrados pelos COs escolhidos (ver aplicarFiltroCo).
  avisosVisiveis: [],
  contasVisiveis: [],
});
const ehAdmin = () => estado.acesso?.papel === "admin" && estado.acesso?.status === "aprovado";
const ehSuperAdmin = () => ehAdmin() && !!estado.acesso?.super_admin;
// Igual a argos_contratos_efetivos() no banco: o administrador geral vê todos.
const contratosDe = (a) => (a?.super_admin ? CONTRATOS : (a?.contratos || []).filter((c) => CONTRATOS.includes(c)));
const multiCo = () => estado.contrato === "negociacao";
const cosSelecionados = () => (multiCo() ? estado.cosPorContrato[estado.contrato] || [] : []);

function salvarPrefs() {
  gravarStorage(localStorage, CHAVE_PREFS, JSON.stringify({
    aba: estado.aba, contrato: estado.contrato, tema: document.documentElement.dataset.theme || null,
    cos: estado.cosPorContrato,
  }));
}

function definirCos(cos) {
  estado.cosPorContrato = { ...estado.cosPorContrato, [estado.contrato]: cos };
  salvarPrefs();
  renderizar();
}

// Contrato da URL (link do push), senão o atual, senão o último usado,
// senão o primeiro liberado.
function escolherContrato() {
  const liberados = contratosDe(estado.acesso);
  const daUrl = new URLSearchParams(location.search).get("contrato");
  for (const c of [daUrl, estado.contrato, prefsSalvas.contrato]) {
    if (liberados.includes(c)) { estado.contrato = c; return; }
  }
  estado.contrato = liberados[0] || null;
}

function trocarContrato(contrato) {
  if (contrato === estado.contrato) return;
  estado.contrato = contrato;
  Object.assign(estado, { snapshot: null, equipe: "", bairro: "", tramite: "", urg: "", equipesResumo: [], avisosEquipe: [] });
  // O ?contrato= do link do push não pode prender o painel no contrato antigo.
  if (location.search) history.replaceState(null, "", location.pathname);
  salvarPrefs();
  renderizar();
  buscarSnapshot();
}

const ABAS_COM_EQUIPE = new Set(["campo", "finalizadas", "avisos"]);
// Na Negociação a equipe é o nome (coluna "Equipe"); `equipe` é a chave.
const SEM_TRAMITE = "-";
const nomeEquipe = (r) => r.equipe_nome || r.equipe;
const normEquipe = (e) => String(e || "").replace(/\s+/g, "").toUpperCase();

// ------------------------------------------------------------------
// API
// ------------------------------------------------------------------
async function buscarSnapshot() {
  if (!estado.acesso) return false;
  const btn = $("#btn-atualizar");
  btn.classList.add("girando");
  try {
    const token = await tokenAtual();
    if (!token) { await sair("Sessão expirada. Entre novamente."); return false; }
    const contrato = estado.contrato;
    const r = await fetch(`/api/snapshot?contrato=${encodeURIComponent(contrato)}`, {
      headers: { Authorization: `Bearer ${token}` }, cache: "no-store",
    });
    const corpo = await r.json().catch(() => ({}));
    if (r.status === 401) { await sair(corpo.erro || "Sessão expirada. Entre novamente."); return false; }
    if (r.status === 403) { await verificarAcesso(); return false; }  // revogado enquanto usava
    if (!r.ok) throw new Error(corpo.erro || `Erro ${r.status}`);
    if (contrato !== estado.contrato) return false;  // trocou de contrato no meio da leitura
    estado.snapshot = corpo;
    estado.ultimoErro = null;
    if (!estado.pushSincronizado) { estado.pushSincronizado = true; sincronizarPush(); }
    await Promise.all([carregarDadosEquipes(), ehAdmin() ? carregarAcessos() : null]);
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

// Contas das equipes e avisos do contrato exibido (migrações 003 e 004).
// Sem as migrações aplicadas, as chamadas falham e o painel segue sem esses dados.
async function carregarDadosEquipes() {
  const contrato = estado.contrato;
  const desde = new Date(Date.now() - 7 * 86400000).toISOString();
  const [resumo, avisos] = await Promise.all([
    estado.sb.rpc("argos_equipes_resumo"),
    estado.sb.from("argos_avisos_equipe").select("*").eq("contrato", contrato).gte("criado_em", desde)
      .order("criado_em", { ascending: false }).limit(3000),
  ]);
  if (contrato !== estado.contrato) return;
  if (!resumo.error) estado.equipesResumo = (resumo.data || []).filter((c) => c.contrato === contrato);
  if (!avisos.error) estado.avisosEquipe = avisos.data || [];
}

// Ações com a service role, pela API do Vercel (redefinir senha / excluir conta de equipe).
async function acaoContaEquipe(acao, alvo, equipe) {
  const token = await tokenAtual();
  const r = await fetch("/api/admin-equipe", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ acao, alvo }),
  });
  const corpo = await r.json().catch(() => ({}));
  if (!r.ok) { alert(corpo.erro || `Erro ${r.status}`); return; }
  if (acao === "redefinir_senha") {
    prompt(`Senha temporária da equipe ${equipe}. Copie e repasse à equipe; ela vai criar uma senha nova no próximo login:`, corpo.senha);
  }
  await Promise.all([carregarAcessos(), carregarDadosEquipes()]);
  renderizar();
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
  const textos = {
    recusado: ["Acesso não liberado",
      "Seu acesso ao ARGOS foi recusado ou revogado por um administrador. Fale com a coordenação se achar que é um engano."],
    sem_contrato: ["Aguardando liberação de contrato",
      "Seu acesso foi aprovado, mas ainda não há painel (SOC ou Negociação) liberado para você. Assim que o administrador geral liberar, é só clicar em “Verificar novamente”."],
  };
  const [titulo, texto] = textos[status] || ["Aguardando aprovação",
    "Seu pedido de acesso foi registrado. Assim que um administrador aprovar, é só clicar em “Verificar novamente”."];
  $("#espera-titulo").textContent = titulo;
  $("#espera-texto").textContent = texto;
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
  // Contas de equipe usam o painel próprio, só com as ordens delas.
  if (data?.papel === "equipe") { location.replace("/equipe"); return; }
  estado.acesso = data;
  if (data?.status === "aprovado" && !contratosDe(data).length) {
    mostrarEspera("sem_contrato", sessao.session.user.email);
  } else if (data?.status === "aprovado") {
    const anterior = estado.contrato;
    escolherContrato();
    if ($("#tela-painel").hidden) iniciarPainel();
    else if (anterior !== estado.contrato) { estado.snapshot = null; buscarSnapshot(); }  // perdeu o contrato exibido
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
  const todas = {
    campo: enriquecer(snap?.campo?.registros, agora),
    programaveis: enriquecer(snap?.programaveis?.registros, agora),
    finalizadas: enriquecerFinalizadas(snap?.campo?.finalizadas),
  };
  const { campo, programaveis, finalizadas, totalProgramaveis } = aplicarFiltroCo(snap, todas);

  renderContrato();
  renderFiltroCos(snap, todas);
  renderStatus(snap, agora);
  renderExtracao(snap, campo, agora);
  renderAvisos(snap, agora);
  renderKpis(snap, campo, programaveis, finalizadas, totalProgramaveis, agora);
  renderAbas(campo, programaveis, finalizadas, agora);
  renderFiltros(campo, finalizadas, programaveis);
  if (estado.aba === "acessos" && !ehAdmin()) estado.aba = "campo";
  renderTabela(campo, programaveis, finalizadas, agora);
  $("#usuario-atual").textContent = estado.acesso?.email || "";

  $("#rodape-info").textContent = snap?.gerado_em
    ? `Snapshot gerado às ${fmtHora.format(data(snap.gerado_em))} · atualiza a cada 60s · cores recalculadas a cada 30s`
    : "";
}

// Negociação: os COs escolhidos valem para tudo (indicadores, abas, equipes,
// avisos e Excel). Cada CO é acompanhado por pessoas diferentes.
function aplicarFiltroCo(snap, todas) {
  const sel = cosSelecionados();
  if (!sel.length) {
    estado.avisosVisiveis = estado.avisosEquipe;
    estado.contasVisiveis = estado.equipesResumo;
    return { ...todas, totalProgramaveis: snap?.programaveis?.total ?? todas.programaveis.length };
  }
  const noCo = (r) => sel.includes(r.co || "");
  const vis = {
    campo: todas.campo.filter(noCo),
    programaveis: todas.programaveis.filter(noCo),
    finalizadas: todas.finalizadas.filter(noCo),
  };
  // Avisos e contas de equipe não trazem o CO: vem do TdC do aviso ou dos
  // COs em que a equipe aparece no relatório.
  const coDoTdc = new Map();
  const cosDaEquipe = new Map();
  for (const r of [...todas.campo, ...todas.programaveis, ...todas.finalizadas]) {
    if (r.tdc) coDoTdc.set(String(r.tdc), r.co || "");
    if (r.equipe) {
      if (!cosDaEquipe.has(r.equipe)) cosDaEquipe.set(r.equipe, new Set());
      cosDaEquipe.get(r.equipe).add(r.co || "");
    }
  }
  const equipeNoCo = (e) => [...(cosDaEquipe.get(e) || [])].some((c) => sel.includes(c));
  estado.avisosVisiveis = estado.avisosEquipe.filter((a) =>
    coDoTdc.has(String(a.tdc)) ? sel.includes(coDoTdc.get(String(a.tdc))) : equipeNoCo(a.equipe));
  estado.contasVisiveis = estado.equipesResumo.filter((c) => equipeNoCo(c.equipe));
  // O total declarado pelo eOrder inclui as páginas da grade que não são lidas.
  const totais = snap?.programaveis?.total_por_co || {};
  vis.totalProgramaveis = sel.reduce((s, co) => s + (totais[co] ?? vis.programaveis.filter((r) => r.co === co).length), 0);
  return vis;
}

function renderFiltroCos(snap, todas) {
  const barra = $("#filtro-cos");
  barra.hidden = !multiCo();
  if (!multiCo()) return;
  const emCampo = new Map();
  for (const r of todas.campo) emCampo.set(r.co || "", (emCampo.get(r.co || "") || 0) + 1);
  const cos = new Set([...todas.campo, ...todas.programaveis, ...todas.finalizadas].map((r) => r.co || ""));
  for (const co of Object.keys(snap?.programaveis?.total_por_co || {})) cos.add(co);
  const sel = cosSelecionados();
  for (const co of sel) cos.add(co);  // continua visível (e desmarcável) mesmo sem ordens agora
  const lista = [...cos].filter(Boolean).sort((a, b) => nomeCo(a).localeCompare(nomeCo(b), "pt-BR"));
  const html = [`<button class="chip" data-co="" aria-pressed="${!sel.length}">Todos</button>`]
    .concat(lista.map((co) => `<button class="chip" data-co="${esc(co)}" aria-pressed="${sel.includes(co)}" title="${esc(co)}">`
      + `${esc(nomeCo(co))} <span class="contador">${emCampo.get(co) || 0}</span></button>`))
    .join("");
  const chips = $("#filtro-cos-chips");
  if (chips.dataset.html !== html) { chips.innerHTML = html; chips.dataset.html = html; }
}

function renderContrato() {
  const liberados = contratosDe(estado.acesso);
  const sel = $("#seletor-contrato");
  sel.hidden = liberados.length < 2;
  const html = liberados
    .map((c) => `<button class="chip" data-contrato="${c}">${esc(NOMES_CONTRATO[c])}</button>`).join("");
  if (sel.dataset.html !== html) { sel.innerHTML = html; sel.dataset.html = html; }
  for (const b of $$("#seletor-contrato .chip")) b.setAttribute("aria-pressed", String(b.dataset.contrato === estado.contrato));
  const nome = NOMES_CONTRATO[estado.contrato] || "";
  $("#subtitulo-painel").textContent = `Religações · ${nome}`;
  document.title = `ARGOS · ${nome || "Religações"}`;
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
      if (e.startsWith("erro") && !e.startsWith("erro parcial")) avisos.push({ erro: true, txt: `${nome}: ${e.slice(0, 240)}` });
      // Negociação: um CO que falhou na última leitura mantém os dados anteriores.
      const cos = snap[chave]?.cos_com_erro || [];
      if (cos.length) avisos.push({ txt: `${nome}: sem dados novos de ${cos.map(nomeCo).join(", ")} na última leitura (ficaram os anteriores).` });
    }
  }
  $("#avisos").innerHTML = avisos.map((a) => `<div class="aviso${a.erro ? " erro" : ""}">${esc(a.txt)}</div>`).join("");
}

function renderKpis(snap, campo, programaveis, finalizadas, totalProg, agora) {
  const conta = (f) => campo.filter((r) => f(r._min)).length;
  const vencidas = conta(FILTRO_URGENCIA.vencida);
  const ate30 = conta(FILTRO_URGENCIA.critico);
  const ate60 = conta(FILTRO_URGENCIA.alerta);
  const hoje = campo.filter((r) => r._min > 0 && mesmoDia(r._venc, agora)).length;
  const novasProg = programaveis.filter((r) => {
    const d = data(r.primeiro_visto_em);
    return d && agora - d <= 60 * 60000;
  }).length;
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
    kpiAvisos(agora),
    kpi({ rotulo: "Programáveis", valor: totalProg, detalhe: novasProg ? `${novasProg} nova(s) em 1h` : "aguardando designação", cor: totalProg ? "atencao" : null, acao: "programaveis:" }),
  ].join("");
}

// Avisos às equipes ainda sem confirmação (só aparece com contas de equipe).
function kpiAvisos(agora) {
  if (!estado.equipesResumo.length && !estado.avisosEquipe.length) return "";
  const pendentes = estado.avisosVisiveis.filter((a) => a.status === "pendente");
  const equipes = new Set(pendentes.map((a) => a.equipe)).size;
  const maisAntigo = pendentes.reduce((m, a) => (!m || a.criado_em < m ? a.criado_em : m), null);
  return kpi({
    rotulo: "Avisos pendentes",
    valor: pendentes.length,
    detalhe: pendentes.length ? `${equipes} equipe(s) · ${haQuanto(data(maisAntigo), agora)}` : "equipes confirmaram tudo",
    cor: pendentes.length ? "vencida" : "ok",
    acao: "avisos:",
  });
}

function renderAbas(campo, programaveis, finalizadas, agora) {
  const finHoje = finalizadas.filter((r) => finalizadaHoje(r, agora));
  $("#cont-campo").textContent = campo.length;
  $("#cont-programaveis").textContent = programaveis.length;
  $("#cont-finalizadas").textContent = finHoje.length;
  $("#cont-equipes").textContent = new Set([...campo, ...finHoje].map((r) => r.equipe || "")).size;
  $("#aba-acessos").hidden = !ehAdmin();
  const temEquipes = estado.equipesResumo.length > 0 || estado.avisosEquipe.length > 0;
  $("#aba-avisos").hidden = !temEquipes;
  if (estado.aba === "avisos" && !temEquipes) estado.aba = "campo";
  const avisosPendentes = estado.avisosVisiveis.filter((a) => a.status === "pendente").length;
  $("#cont-avisos").textContent = avisosPendentes;
  $("#cont-avisos").style.color = avisosPendentes ? "var(--vencida)" : "";
  const pendentes = estado.acessos.filter((a) => a.status === "pendente").length;
  $("#cont-acessos").textContent = pendentes;
  $("#cont-acessos").style.color = pendentes ? "var(--alerta)" : "";
  for (const b of $$(".aba")) b.setAttribute("aria-selected", String(b.dataset.aba === estado.aba));
}

function preencherSelect(sel, valores, atual, rotuloTodos, rotulo = (v) => v) {
  const opcoes = [`<option value="">${rotuloTodos}</option>`]
    .concat(valores.map((v) => `<option value="${esc(v)}"${v === atual ? " selected" : ""}>${esc(rotulo(v) || "(sem)")}</option>`));
  const html = opcoes.join("");
  if (sel.dataset.html !== html) { sel.innerHTML = html; sel.dataset.html = html; }
  sel.value = atual;
}

function renderFiltros(campo, finalizadas, programaveis) {
  $("#filtros").hidden = estado.aba === "equipes" || estado.aba === "acessos";
  const comEquipe = ABAS_COM_EQUIPE.has(estado.aba);
  const ehFinalizadas = estado.aba === "finalizadas";
  $("#filtro-equipe").hidden = !comEquipe;
  const ehAvisos = estado.aba === "avisos";
  $("#filtro-bairro").hidden = !comEquipe || ehAvisos;
  $("#filtro-tramite").hidden = !comEquipe || ehAvisos;
  $("#filtro-urgencia").hidden = ehFinalizadas || ehAvisos;
  $("#filtro-periodo").hidden = !ehFinalizadas;
  $("#filtro-status-aviso").hidden = !ehAvisos;
  const base = ehAvisos ? estado.avisosVisiveis : ehFinalizadas ? finalizadas : campo;
  const unicos = (campo_) => [...new Set(base.map((r) => r[campo_] || ""))].sort((a, b) => a.localeCompare(b, "pt-BR"));
  preencherSelect($("#filtro-equipe"), unicos("equipe"), estado.equipe, "Todas as equipes");
  preencherSelect($("#filtro-bairro"), unicos("bairro"), estado.bairro, "Todos os bairros");
  const tramites = [...new Set(base.map((r) => r._tramite).filter(Boolean))].sort((a, b) => a.localeCompare(b, "pt-BR"));
  if (base.some((r) => !r._tramite)) tramites.push(SEM_TRAMITE);
  preencherSelect($("#filtro-tramite"), tramites, estado.tramite, "Todos os trâmites", (v) => (v === SEM_TRAMITE ? "Sem indicação" : v));
  for (const c of $$("#filtro-urgencia .chip")) c.setAttribute("aria-pressed", String(c.dataset.urg === estado.urg));
  for (const c of $$("#filtro-periodo .chip")) c.setAttribute("aria-pressed", String(c.dataset.periodo === estado.periodo));
  for (const c of $$("#filtro-status-aviso .chip")) c.setAttribute("aria-pressed", String(c.dataset.status === estado.statusAviso));
}

// ---------- colunas ----------
const celRestante = (r) => `<span class="badge">${esc(textoRestante(r._min))}</span>`;
// "Ceneged - Angra (CENEGED_ANGRA)" -> "Angra".
// O de Petrópolis vem cortado no eOrder, sem o ")": "Ceneged - Petrópolis (CENEGED_PETROPOLIS".
const nomeCo = (co) => String(co || "").replace(/^\s*ceneged\s*-\s*/i, "").replace(/\s*\([^)]*\)?\s*$/, "");
// Coluna CO: só no painel da Negociação (ver colunasDa).
const COLUNA_CO = { id: "co", rotulo: "CO", multiCo: true, valor: (r) => nomeCo(r.co), html: (r) => esc(nomeCo(r.co)) };
// "Ciclo de trabalho" da planilha ("Ciclo 15 - Religação Negociador Digital");
// o código do serviço (OUTREA/OUTREL) fica embaixo.
const COLUNA_TIPO_CAMPO = { id: "tipo", rotulo: "Tipo", valor: (r) => r.tipo, html: (r) => `${esc(r.tipo)}${r.tipo_servico ? `<div class="secundario">${esc(r.tipo_servico)}</div>` : ""}`, cheio: true };
// Medidor, Poste DAT, Ramal..., tirado do fim do ciclo (tramiteDoCiclo).
const COLUNA_TRAMITE = { id: "tramite", rotulo: "Trâmite", valor: (r) => r._tramite, html: (r) => r._tramite ? esc(r._tramite) : `<span class="fraco">—</span>` };
const COLUNAS = {
  campo: [
    { id: "restante", rotulo: "Tempo restante", valor: (r) => r._min, html: celRestante },
    { id: "vencimento", rotulo: "Vencimento", valor: (r) => r._min, html: (r) => `<span class="num">${esc(diaHora(r._venc))}</span>` },
    COLUNA_CO,
    { id: "ordem", rotulo: "Ordem", valor: (r) => r.ordem, html: (r) => `<span class="num">${esc(r.ordem)}</span>` },
    { id: "tdc", rotulo: "TdC", valor: (r) => r.tdc, html: (r) => `<span class="num">${esc(r.tdc)}</span>` },
    { id: "cliente", rotulo: "Cliente", valor: (r) => r.cliente, html: (r) => `<span class="num">${esc(r.cliente)}</span>${r.nome_cliente ? `<div class="secundario">${esc(r.nome_cliente)}</div>` : ""}` },
    { id: "equipe", rotulo: "Equipe", valor: (r) => nomeEquipe(r), html: (r) => r.equipe ? esc(nomeEquipe(r)) : `<span class="fraco">sem equipe</span>` },
    { id: "bairro", rotulo: "Bairro", valor: (r) => r.bairro, html: (r) => `${esc(r.bairro)}${r.endereco ? `<div class="secundario celula-endereco" title="${esc(r.endereco)}">${esc(r.endereco)}</div>` : ""}`, cheio: true },
    COLUNA_TIPO_CAMPO,
    COLUNA_TRAMITE,
  ],
  programaveis: [
    { id: "restante", rotulo: "Tempo restante", valor: (r) => r._min, html: celRestante },
    { id: "vencimento", rotulo: "Vencimento", valor: (r) => r._min, html: (r) => `<span class="num">${esc(diaHora(r._venc))}</span>` },
    COLUNA_CO,
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
    COLUNA_CO,
    { id: "ordem", rotulo: "Ordem", valor: (r) => r.ordem, html: (r) => `<span class="num">${esc(r.ordem)}</span>` },
    { id: "tdc", rotulo: "TdC", valor: (r) => r.tdc, html: (r) => `<span class="num">${esc(r.tdc)}</span>` },
    { id: "cliente", rotulo: "Cliente", valor: (r) => r.cliente, html: (r) => `<span class="num">${esc(r.cliente)}</span>${r.nome_cliente ? `<div class="secundario">${esc(r.nome_cliente)}</div>` : ""}` },
    { id: "equipe", rotulo: "Equipe", valor: (r) => nomeEquipe(r), html: (r) => esc(nomeEquipe(r)) },
    { id: "bairro", rotulo: "Bairro", valor: (r) => r.bairro, html: (r) => `${esc(r.bairro)}${r.endereco ? `<div class="secundario celula-endereco" title="${esc(r.endereco)}">${esc(r.endereco)}</div>` : ""}`, cheio: true },
    COLUNA_TIPO_CAMPO,
    COLUNA_TRAMITE,
    { id: "resultado", rotulo: "Resultado", valor: (r) => r.resultado, html: (r) => `${esc(r.resultado || "—")}${r.causa ? `<div class="secundario">${esc(r.causa)}</div>` : ""}`, cheio: true },
  ],
  equipes: [
    { id: "equipe", rotulo: "Equipe", valor: (r) => r.nome || r.equipe, html: (r) => `<button class="link-equipe" data-equipe="${esc(r.equipe)}">${esc(r.nome || r.equipe || "(sem equipe)")}</button>` },
    { id: "total", rotulo: "Em campo", valor: (r) => r.total, html: (r) => `<span class="num">${r.total}</span>` },
    { id: "vencidas", rotulo: "Vencidas", valor: (r) => r.vencidas, html: (r) => `<span class="num" style="color:${r.vencidas ? "var(--vencida)" : "var(--texto-3)"}">${r.vencidas}</span>` },
    { id: "finalizadas", rotulo: "Finalizadas hoje", valor: (r) => r.finalizadas, html: (r) => `<span class="num" style="color:${r.finalizadas ? "var(--ok)" : "var(--texto-3)"}">${r.finalizadas}</span>` },
    { id: "ate60", rotulo: "Vencem ≤ 1h", valor: (r) => r.ate60, html: (r) => `<span class="num" style="color:${r.ate60 ? "var(--alerta)" : "var(--texto-3)"}">${r.ate60}</span>` },
    { id: "restante", rotulo: "Próximo vencimento", valor: (r) => r._min, html: (r) => r._min == null ? `<span class="fraco">—</span>` : celRestante(r) },
    { id: "visto", rotulo: "Última visualização", valor: (r) => r.conta?.ultimo_acesso_em, html: (r, agora) => celVisto(r.conta, agora) },
    { id: "pendentes", rotulo: "Avisos pendentes", valor: (r) => r.conta ? r.conta.pendentes : null, html: (r, agora) => celPendentes(r.conta, agora) },
    { id: "aparelhos", rotulo: "Push", valor: (r) => r.conta ? r.conta.aparelhos : null, html: (r) => celAparelhos(r.conta) },
    { id: "distribuicao", rotulo: "Distribuição", valor: null, html: (r) => {
      const seg = ["vencida", "critico", "alerta", "atencao", "ok", "sem"]
        .filter((u) => r.dist[u])
        .map((u) => `<span style="width:${(r.dist[u] / (r.total || 1)) * 100}%;background:var(--${u})" title="${u}: ${r.dist[u]}"></span>`)
        .join("");
      return `<div class="barra">${seg}</div>`;
    }, cheio: true },
  ],
};

const colunasDa = (aba) => COLUNAS[aba].filter((c) => !c.multiCo || multiCo());

// Conta da equipe no painel /equipe (argos_equipes_resumo).
function celVisto(conta, agora) {
  if (!conta) return `<span class="fraco">sem acesso</span>`;
  if (conta.status !== "aprovado") return `<span class="tag ${esc(conta.status)}">${esc(conta.status)}</span>`;
  const d = data(conta.ultimo_acesso_em);
  return d ? `<span class="num">${esc(diaHora(d))}</span><div class="secundario">${esc(haQuanto(d, agora))}</div>` : `<span class="fraco">nunca abriu</span>`;
}
function celPendentes(conta, agora) {
  if (!conta) return `<span class="fraco">—</span>`;
  if (!conta.pendentes) return `<span class="num" style="color:var(--texto-3)">0</span>`;
  return `<span class="num" style="color:var(--vencida);font-weight:600">${conta.pendentes}</span><div class="secundario">desde ${esc(haQuanto(data(conta.pendente_desde), agora).replace(/^há /, ""))}</div>`;
}
function celAparelhos(conta) {
  if (!conta) return `<span class="fraco">—</span>`;
  return conta.aparelhos
    ? `<span class="num">${conta.aparelhos}</span>`
    : `<span class="num" style="color:var(--alerta)" title="Nenhum celular da equipe ativou as notificações">0</span>`;
}

const NOME_TIPO_AVISO = { designada: "Designada", vencer: "Vai vencer", vencida: "Vencida" };
function textoTipoAviso(a) {
  if (a.tipo === "vencer") {
    const m = /^(\d+)min$/.exec(a.nivel || "");
    return m ? `Vence em ≤ ${duracao(Number(m[1]))}` : NOME_TIPO_AVISO.vencer;
  }
  return NOME_TIPO_AVISO[a.tipo] || a.tipo;
}
function textoTempoConfirmacao(a, agora) {
  const criado = data(a.criado_em);
  if (a.status === "confirmado") return duracao((data(a.confirmado_em) - criado) / 60000);
  if (a.status === "pendente") return `há ${duracao((agora - criado) / 60000)}`;
  return "—";
}
const ORDEM_STATUS_AVISO = { pendente: 0, confirmado: 1, dispensado: 2 };
COLUNAS.avisos = [
  { id: "enviado", rotulo: "Enviado em", valor: (a) => a.criado_em, html: (a) => `<span class="num">${esc(diaHora(data(a.criado_em)))}</span>` },
  { id: "equipe", rotulo: "Equipe", valor: (a) => a.equipe, html: (a) => esc(a.equipe) },
  { id: "tipo", rotulo: "Tipo", valor: (a) => textoTipoAviso(a), html: (a) => esc(textoTipoAviso(a)) },
  { id: "tdc", rotulo: "TdC", valor: (a) => a.tdc, html: (a) => `<span class="num">${esc(a.tdc)}</span>${a.ordem ? `<div class="secundario num">${esc(a.ordem)}</div>` : ""}` },
  { id: "status", rotulo: "Status", valor: (a) => ORDEM_STATUS_AVISO[a.status], html: (a) => `<span class="tag ${esc(a.status)}">${esc(a.status)}</span>` },
  { id: "confirmado", rotulo: "Confirmado em", valor: (a) => a.confirmado_em, html: (a) => a.confirmado_em ? `<span class="num">${esc(diaHora(data(a.confirmado_em)))}</span>` : `<span class="fraco">—</span>` },
  { id: "tempo", rotulo: "Tempo até confirmar", valor: (a) => (a.status === "confirmado" ? data(a.confirmado_em) - data(a.criado_em) : null), html: (a, agora) => `<span class="badge">${esc(textoTempoConfirmacao(a, agora))}</span>` },
  { id: "reenvios", rotulo: "Reenvios", valor: (a) => a.reenvios, html: (a) => `<span class="num">${a.reenvios || 0}</span>` },
  { id: "aviso", rotulo: "Aviso", valor: (a) => a.titulo, html: (a) => `${esc(a.titulo)}${a.mensagem ? `<div class="secundario">${esc(a.mensagem)}</div>` : ""}`, cheio: true },
];
// Pendente há mais de 15 min fica vermelho; confirmado, verde.
function urgAviso(a, agora) {
  if (a.status === "confirmado") return "ok";
  if (a.status === "dispensado") return "sem";
  return agora - data(a.criado_em) > 15 * 60000 ? "vencida" : "alerta";
}

const ORDEM_STATUS = { pendente: 0, aprovado: 1, recusado: 2 };
COLUNAS.acessos = [
  { id: "nome", rotulo: "Usuário", valor: (a) => a.equipe || a.nome || a.email, html: (a, agora) => {
    if (a.papel === "equipe") {
      const conta = estado.equipesResumo.find((c) => c.user_id === a.user_id);
      const reset = a.reset_solicitado_em
        ? `<div class="secundario" style="color:var(--alerta)">pediu reset de senha ${esc(haQuanto(data(a.reset_solicitado_em), agora))}</div>` : "";
      const push = conta ? `<div class="secundario">push em ${conta.aparelhos} aparelho(s)</div>` : "";
      const nome = a.nome && a.nome !== a.equipe ? `<div class="secundario">${esc(a.nome)}</div>` : "";
      return `${esc(a.equipe)} <span class="tag equipe">equipe</span>${nome}${reset}${push}`;
    }
    const papel = a.super_admin ? ` <span class="tag admin">admin geral</span>` : a.papel === "admin" ? ` <span class="tag admin">admin</span>` : "";
    return `${esc(a.nome || a.email)}${papel}${a.nome ? `<div class="secundario">${esc(a.email)}</div>` : ""}`;
  } },
  { id: "status", rotulo: "Status", valor: (a) => ORDEM_STATUS[a.status], html: (a) => `<span class="tag ${esc(a.status)}">${esc(a.status)}</span>` },
  // Painéis liberados. Só o administrador geral muda (argos_definir_contratos).
  { id: "contratos", rotulo: "Painéis", valor: (a) => (a.papel === "equipe" ? a.contrato : contratosDe(a).join(",")), html: (a) => {
    if (a.papel === "equipe") return `<span class="tag">${esc(NOMES_CONTRATO[a.contrato] || a.contrato || "")}</span>`;
    if (a.super_admin) return `<span class="secundario">todos</span>`;
    const atuais = contratosDe(a);
    if (!ehSuperAdmin()) {
      return atuais.length ? atuais.map((c) => `<span class="tag">${esc(NOMES_CONTRATO[c])}</span>`).join(" ") : `<span class="fraco">nenhum</span>`;
    }
    return `<div class="acoes-linha">${CONTRATOS.map((c) => {
      const tem = atuais.includes(c);
      const novos = tem ? atuais.filter((x) => x !== c) : [...atuais, c];
      return `<button class="btn-mini${tem ? " aprovar" : ""}" aria-pressed="${tem}" data-fn="argos_definir_contratos" data-alvo="${esc(a.user_id)}" data-contratos="${esc(novos.join(","))}" title="${tem ? "Tirar" : "Liberar"} o painel ${esc(NOMES_CONTRATO[c])}">${tem ? "✓ " : ""}${esc(NOMES_CONTRATO[c])}</button>`;
    }).join("")}</div>`;
  } },
  { id: "solicitado", rotulo: "Pediu acesso", valor: (a) => a.solicitado_em, html: (a, agora) => `<span class="secundario">${esc(haQuanto(data(a.solicitado_em), agora))}</span>` },
  { id: "ultimo", rotulo: "Último acesso", valor: (a) => a.ultimo_acesso_em, html: (a, agora) => `<span class="secundario">${esc(a.ultimo_acesso_em ? haQuanto(data(a.ultimo_acesso_em), agora) : "nunca")}</span>` },
  { id: "acoes", rotulo: "Ações", valor: null, cheio: true, html: (a) => {
    const id = esc(a.user_id);
    const eu = a.user_id === estado.acesso?.user_id;
    const b = [];
    if (a.super_admin) return `<span class="fraco">—</span>`;  // ninguém altera o administrador geral
    if (a.status !== "aprovado") b.push(`<button class="btn-mini aprovar" data-fn="argos_definir_status" data-alvo="${id}" data-valor="aprovado">Aprovar</button>`);
    if (a.status === "pendente") b.push(`<button class="btn-mini recusar" data-fn="argos_definir_status" data-alvo="${id}" data-valor="recusado">Recusar</button>`);
    if (a.status === "aprovado" && !eu) b.push(`<button class="btn-mini recusar" data-fn="argos_definir_status" data-alvo="${id}" data-valor="recusado" data-confirmar="Revogar o acesso de ${esc(a.email)}?">Revogar</button>`);
    if (a.papel === "equipe") {
      const eq = esc(a.equipe);
      b.push(`<button class="btn-mini${a.reset_solicitado_em ? " destaque" : ""}" data-conta-equipe="redefinir_senha" data-alvo="${id}" data-nome="${eq}" data-confirmar="Gerar uma senha temporária para a equipe ${eq}? A senha atual deixa de funcionar.">Redefinir senha</button>`);
      b.push(`<button class="btn-mini recusar" data-conta-equipe="excluir" data-alvo="${id}" data-nome="${eq}" data-confirmar="Excluir a conta da equipe ${eq}? O código fica livre para um novo cadastro.">Excluir conta</button>`);
      return `<div class="acoes-linha">${b.join("")}</div>`;
    }
    // Promover/rebaixar admins: só o administrador geral.
    if (!ehSuperAdmin()) return `<div class="acoes-linha">${b.join("")}</div>`;
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
  const nomes = new Map();
  for (const r of [...campo, ...finalizadasHoje]) if (r.equipe_nome) nomes.set(r.equipe, r.equipe_nome);
  for (const c of estado.contasVisiveis) if (c.nome && !nomes.has(c.equipe)) nomes.set(c.equipe, c.nome);
  for (const r of finalizadasHoje) grupo(r.equipe || "").finalizadas++;
  for (const c of estado.contasVisiveis) if (c.pendentes) grupo(c.equipe);
  for (const r of campo) {
    const g = grupo(r.equipe || "");
    g.total++;
    if (FILTRO_URGENCIA.vencida(r._min)) g.vencidas++;
    if (FILTRO_URGENCIA.alerta(r._min)) g.ate60++;
    g.dist[r._urg] = (g.dist[r._urg] || 0) + 1;
    if (r._min != null && r._min > 0 && (g._min == null || r._min < g._min)) g._min = r._min;
  }
  // Equipe com vencida em aberto fica vermelha mesmo que a próxima ainda esteja no prazo.
  const contas = new Map(estado.contasVisiveis.map((c) => [normEquipe(c.equipe), c]));
  return [...grupos.values()].map((g) => ({
    ...g, nome: nomes.get(g.equipe) || null,
    conta: contas.get(normEquipe(g.equipe)) || null, _urg: g.vencidas ? "vencida" : classificar(g._min),
  }));
}

function filtrar(linhas, aba = estado.aba) {
  const termo = estado.texto.trim().toLowerCase();
  const agora = new Date();
  return linhas.filter((r) => {
    if (ABAS_COM_EQUIPE.has(aba)) {
      if (estado.equipe && (r.equipe || "") !== estado.equipe) return false;
      if (aba !== "avisos" && estado.bairro && (r.bairro || "") !== estado.bairro) return false;
      if (aba !== "avisos" && estado.tramite && (r._tramite || SEM_TRAMITE) !== estado.tramite) return false;
    }
    if (aba === "avisos") {
      if (estado.statusAviso && r.status !== estado.statusAviso) return false;
    } else if (aba === "finalizadas") {
      if (estado.periodo === "hoje" && !finalizadaHoje(r, agora)) return false;
    } else if (estado.urg && !FILTRO_URGENCIA[estado.urg](r._min)) return false;
    if (termo) {
      const alvo = [r.ordem, r.tdc, r.cliente, r.equipe, r.equipe_nome, r.co, r.bairro, r.tipo, r.tipo_servico, r.endereco, r.nome_cliente, r.resultado, r.titulo].join(" ").toLowerCase();
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
  else if (aba === "avisos") {
    const agora = new Date();
    linhas = filtrar(estado.avisosVisiveis.map((a) => ({ ...a, _urg: urgAviso(a, agora) })), aba);
  } else if (aba === "acessos") linhas = estado.acessos.map((a) => ({ ...a, _urg: a.status === "pendente" ? "alerta" : a.status === "recusado" ? "sem" : "ok" }));
  else {
    const agora = new Date();
    linhas = agruparPorEquipe(campo, finalizadas.filter((r) => finalizadaHoje(r, agora)));
  }
  return ordenar(linhas, colunasDa(aba), aba);
}

function renderTabela(campo, programaveis, finalizadas, agora) {
  const colunas = colunasDa(estado.aba);
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
      : estado.aba === "avisos"
      ? (estado.avisosVisiveis.length ? "Nenhum aviso com esses filtros." : "Nenhum aviso às equipes nos últimos 7 dias.")
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
    ["CO", (r) => nomeCo(r.co), "co"],
    ["Ordem", (r) => r.ordem],
    ["TdC", (r) => r.tdc],
    ["Cliente", (r) => r.cliente],
    ["Nome do cliente", (r) => r.nome_cliente],
    ["Equipe", (r) => nomeEquipe(r)],
    ["Bairro", (r) => r.bairro],
    ["Endereço", (r) => r.endereco],
    ["Tipo", (r) => r.tipo],
    ["Serviço", (r) => r.tipo_servico],
    ["Trâmite", (r) => r._tramite],
    ["Vencimento", (r) => dataBrasilia(r._venc), "data"],
    ["Tempo restante", (r) => textoRestante(r._min), "urgencia"],
    ["Situação", (r) => NOME_URGENCIA[r._urg], "urgencia"],
  ],
  programaveis: [
    ["CO", (r) => nomeCo(r.co), "co"],
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
    ["CO", (r) => nomeCo(r.co), "co"],
    ["Ordem", (r) => r.ordem],
    ["TdC", (r) => r.tdc],
    ["Cliente", (r) => r.cliente],
    ["Nome do cliente", (r) => r.nome_cliente],
    ["Equipe", (r) => nomeEquipe(r)],
    ["Bairro", (r) => r.bairro],
    ["Endereço", (r) => r.endereco],
    ["Tipo", (r) => r.tipo],
    ["Serviço", (r) => r.tipo_servico],
    ["Trâmite", (r) => r._tramite],
    ["Vencimento", (r) => dataBrasilia(r._venc), "data"],
    ["Finalizada em", (r) => dataBrasilia(r._fim), "data"],
    ["Situação", (r) => textoSituacaoFinalizada(r), "urgencia"],
    ["Resultado", (r) => r.resultado],
    ["Causa", (r) => r.causa],
  ],
  avisos: [
    ["Equipe", (a) => a.equipe],
    ["Tipo", (a) => textoTipoAviso(a)],
    ["TdC", (a) => a.tdc],
    ["Ordem", (a) => a.ordem],
    ["Aviso", (a) => a.titulo],
    ["Detalhe", (a) => a.mensagem],
    ["Enviado em", (a) => dataBrasilia(data(a.criado_em)), "data"],
    ["Status", (a) => a.status, "urgencia"],
    ["Confirmado em", (a) => dataBrasilia(data(a.confirmado_em)), "data"],
    ["Tempo até confirmar", (a) => (a.status === "confirmado" ? textoTempoConfirmacao(a, new Date()) : "")],
    ["Reenvios", (a) => a.reenvios || 0],
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
  else if (aba === "avisos") { if (estado.statusAviso) f.push(`status ${estado.statusAviso}`); }
  else if (estado.urg) f.push($(`#filtro-urgencia [data-urg="${estado.urg}"]`)?.textContent || estado.urg);
  const cos = cosSelecionados();
  if (cos.length) f.push(`${cos.length === 1 ? "CO" : "COs"} ${cos.map(nomeCo).join(", ")}`);
  if (ABAS_COM_EQUIPE.has(aba) && estado.equipe) f.push(`equipe ${estado.equipe}`);
  if (ABAS_COM_EQUIPE.has(aba) && aba !== "avisos" && estado.bairro) f.push(`bairro ${estado.bairro}`);
  if (ABAS_COM_EQUIPE.has(aba) && aba !== "avisos" && estado.tramite) f.push(`trâmite ${estado.tramite === SEM_TRAMITE ? "sem indicação" : estado.tramite}`);
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
  const { campo, programaveis, finalizadas } = aplicarFiltroCo(estado.snapshot, {
    campo: enriquecer(estado.snapshot.campo?.registros, agora),
    programaveis: enriquecer(estado.snapshot.programaveis?.registros, agora),
    finalizadas: enriquecerFinalizadas(estado.snapshot.campo?.finalizadas),
  });

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
    if (estado.avisosVisiveis.length) abas.push(["avisos", "Avisos", "Avisos às equipes (últimos 7 dias)"]);
    for (const [aba, nome, titulo] of abas) {
      const linhas = linhasDaAba(campo, programaveis, finalizadas, aba);
      const resumo = `Gerado em ${geradoEm} · ${linhas.length} ${linhas.length === 1 ? "ordem" : "ordens"} · ${descreverFiltros(aba)}`;
      const colunas = COLUNAS_RELATORIO[aba].filter(([, , tipo]) => tipo !== "co" || multiCo());
      abaPlanilha(wb, nome, `${titulo} · ${NOMES_CONTRATO[estado.contrato]}`, resumo, colunas, linhas);
    }
    const buffer = await wb.xlsx.writeBuffer();
    // ":" não pode em nome de arquivo no Windows, então a hora vai como 14h30.
    const p = partesData(agora);
    baixarArquivo(
      new Blob([buffer], { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" }),
      `Religas ${estado.contrato === "negociacao" ? "Negociação" : "SOC"} - ${p.day}-${p.month}-${p.year} ${p.hour}h${p.minute}.xlsx`,
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
  if (aba === "avisos") estado.statusAviso = "pendente";
  estado.equipe = "";
  estado.bairro = "";
  estado.tramite = "";
  trocarAba(aba);
});

$("#tabela-cabecalho").addEventListener("click", (ev) => {
  const th = ev.target.closest("th[data-col]");
  if (!th) return;
  const o = estado.ordem[estado.aba];
  if (o.col === th.dataset.col) o.dir *= -1;
  else { o.col = th.dataset.col; o.dir = ["vencidas", "total", "ate60", "finalizadas", "finalizada", "visto", "pendentes", "enviado", "reenvios"].includes(th.dataset.col) ? -1 : 1; }
  renderizar();
});

$("#tabela-corpo").addEventListener("click", async (ev) => {
  const conta = ev.target.closest("[data-conta-equipe]");
  if (conta) {
    if (conta.dataset.confirmar && !confirm(conta.dataset.confirmar)) return;
    conta.disabled = true;
    await acaoContaEquipe(conta.dataset.contaEquipe, conta.dataset.alvo, conta.dataset.nome);
    return;
  }
  const acao = ev.target.closest("[data-fn]");
  if (acao) {
    if (acao.dataset.confirmar && !confirm(acao.dataset.confirmar)) return;
    acao.disabled = true;
    const params = { p_alvo: acao.dataset.alvo };
    if (acao.dataset.fn === "argos_definir_contratos") params.p_contratos = acao.dataset.contratos.split(",").filter(Boolean);
    else params[acao.dataset.fn === "argos_definir_status" ? "p_status" : "p_papel"] = acao.dataset.valor;
    await acaoAcesso(acao.dataset.fn, params);
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
$("#filtro-tramite").addEventListener("change", (ev) => { estado.tramite = ev.target.value; renderizar(); });
$("#filtro-cos-chips").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-co]");
  if (!b) return;
  const co = b.dataset.co;
  const atuais = cosSelecionados();
  definirCos(!co ? [] : atuais.includes(co) ? atuais.filter((c) => c !== co) : [...atuais, co]);
});
$("#seletor-contrato").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-contrato]");
  if (b) trocarContrato(b.dataset.contrato);
});
$("#filtro-urgencia").addEventListener("click", (ev) => {
  const c = ev.target.closest(".chip");
  if (!c) return;
  estado.urg = estado.urg === c.dataset.urg ? "" : c.dataset.urg;
  renderizar();
});

$("#filtro-status-aviso").addEventListener("click", (ev) => {
  const c = ev.target.closest(".chip");
  if (!c) return;
  estado.statusAviso = c.dataset.status;
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

// Toque num push com o painel já aberto (sw.js): mostra o contrato do aviso.
navigator.serviceWorker?.addEventListener("message", (ev) => {
  const contrato = ev.data?.tipo === "argos:contrato" ? ev.data.contrato : null;
  if (contrato && contratosDe(estado.acesso).includes(contrato) && !$("#tela-painel").hidden) trocarContrato(contrato);
});

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
    await criarSupabase();
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
