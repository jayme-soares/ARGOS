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
  ordem: {
    campo: { col: "restante", dir: 1 },
    programaveis: { col: "restante", dir: 1 },
    equipes: { col: "vencidas", dir: -1 },
    acessos: { col: "status", dir: 1 },
  },
  sb: null,          // cliente Supabase
  acesso: null,      // linha do usuário em argos_acessos
  acessos: [],       // todas as linhas (só admins)
  ultimoErro: null,
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
function horaOuDia(d, agora) {
  if (!d) return "—";
  return mesmoDia(d, agora) ? fmtHora.format(d) : fmtDiaHora.format(d).replace(",", "");
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

$("#form-login").addEventListener("submit", async (ev) => {
  ev.preventDefault();
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

  renderStatus(snap, agora);
  renderAvisos(snap, agora);
  renderKpis(snap, campo, programaveis, agora);
  renderAbas(campo, programaveis);
  renderFiltros(campo);
  if (estado.aba === "acessos" && !ehAdmin()) estado.aba = "campo";
  renderTabela(campo, programaveis, agora);
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
    let extra = atualizado ? ` · ${fmtHora.format(atualizado)}` : "";
    if (chave === "campo" && snap?.campo?.proxima_extracao && st.cls !== "pausa") {
      extra += ` · próx. ${fmtHora.format(data(snap.campo.proxima_extracao))}`;
    }
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
      if (e.startsWith("erro")) avisos.push({ erro: true, txt: `${nome}: ${e.slice(0, 240)}` });
    }
  }
  $("#avisos").innerHTML = avisos.map((a) => `<div class="aviso${a.erro ? " erro" : ""}">${esc(a.txt)}</div>`).join("");
}

function kpi({ rotulo, valor, detalhe = "", cor = null, acao = null, destaque = false }) {
  const tag = acao ? "button" : "div";
  const estilo = cor ? `style="--cor: var(--${cor}); --cor-valor: var(--${cor})"` : "";
  const zero = valor === 0 ? " zero" : "";
  const dataAcao = acao ? `data-acao="${esc(acao)}"` : "";
  return `<${tag} class="kpi${destaque ? " destaque" : ""}${zero}" ${estilo} ${dataAcao}>
    <div class="rotulo">${esc(rotulo)}</div>
    <div class="valor">${esc(valor)}</div>
    <div class="detalhe">${detalhe}</div>
  </${tag}>`;
}

function renderKpis(snap, campo, programaveis, agora) {
  const conta = (f) => campo.filter((r) => f(r._min)).length;
  const vencidas = conta(FILTRO_URGENCIA.vencida);
  const ate30 = conta(FILTRO_URGENCIA.critico);
  const ate60 = conta(FILTRO_URGENCIA.alerta);
  const hoje = campo.filter((r) => r._min > 0 && mesmoDia(r._venc, agora)).length;
  const novasProg = programaveis.filter((r) => {
    const d = data(r.primeiro_visto_em);
    return d && agora - d <= 60 * 60000;
  }).length;
  const proxima = campo.filter((r) => r._min > 0).sort((a, b) => a._min - b._min)[0];
  const totalProg = snap?.programaveis?.total ?? programaveis.length;

  $("#kpis").innerHTML = [
    kpi({ rotulo: "Em campo", valor: campo.length, detalhe: `exportado ${haQuanto(data(snap?.campo?.atualizado_em), agora)}`, acao: "campo:" }),
    kpi({ rotulo: "Vencidas", valor: vencidas, detalhe: "em aberto no relatório", cor: vencidas ? "vencida" : null, acao: "campo:vencida" }),
    kpi({ rotulo: "Vencem ≤ 30 min", valor: ate30, detalhe: "prioridade máxima", cor: ate30 ? "critico" : null, acao: "campo:critico" }),
    kpi({ rotulo: "Vencem ≤ 1h", valor: ate60, detalhe: "inclui as de ≤ 30 min", cor: ate60 ? "alerta" : null, acao: "campo:alerta" }),
    kpi({ rotulo: "Vencem hoje", valor: hoje, detalhe: "ainda no prazo" }),
    kpi({ rotulo: "Programáveis", valor: totalProg, detalhe: novasProg ? `${novasProg} nova(s) em 1h` : "aguardando designação", cor: totalProg ? "atencao" : null, acao: "programaveis:" }),
    kpi({
      rotulo: "Próximo vencimento",
      valor: proxima ? duracao(proxima._min) : "—",
      detalhe: proxima ? `${fmtHora.format(proxima._venc)} · TdC ${esc(proxima.tdc)} · ${esc(proxima.equipe || "sem equipe")}` : "nenhuma ordem no prazo",
      cor: proxima ? proxima._urg : null,
      destaque: true,
    }),
  ].join("");
}

function renderAbas(campo, programaveis) {
  $("#cont-campo").textContent = campo.length;
  $("#cont-programaveis").textContent = programaveis.length;
  $("#cont-equipes").textContent = new Set(campo.map((r) => r.equipe || "")).size;
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

function renderFiltros(campo) {
  $("#filtros").hidden = estado.aba === "equipes" || estado.aba === "acessos";
  const soCampo = estado.aba === "campo";
  $("#filtro-equipe").hidden = !soCampo;
  $("#filtro-bairro").hidden = !soCampo;
  const unicos = (campo_) => [...new Set(campo.map((r) => r[campo_] || ""))].sort((a, b) => a.localeCompare(b, "pt-BR"));
  preencherSelect($("#filtro-equipe"), unicos("equipe"), estado.equipe, "Todas as equipes");
  preencherSelect($("#filtro-bairro"), unicos("bairro"), estado.bairro, "Todos os bairros");
  for (const c of $$("#filtro-urgencia .chip")) c.setAttribute("aria-pressed", String(c.dataset.urg === estado.urg));
}

// ---------- colunas ----------
const celRestante = (r) => `<span class="badge">${esc(textoRestante(r._min))}</span>`;
const COLUNAS = {
  campo: [
    { id: "restante", rotulo: "Tempo restante", valor: (r) => r._min, html: celRestante },
    { id: "vencimento", rotulo: "Vencimento", valor: (r) => r._min, html: (r, agora) => `<span class="num">${esc(horaOuDia(r._venc, agora))}</span>` },
    { id: "ordem", rotulo: "Ordem", valor: (r) => r.ordem, html: (r) => `<span class="num">${esc(r.ordem)}</span>` },
    { id: "tdc", rotulo: "TdC", valor: (r) => r.tdc, html: (r) => `<span class="num">${esc(r.tdc)}</span>` },
    { id: "cliente", rotulo: "Cliente", valor: (r) => r.cliente, html: (r) => `<span class="num">${esc(r.cliente)}</span>${r.nome_cliente ? `<div class="secundario">${esc(r.nome_cliente)}</div>` : ""}` },
    { id: "equipe", rotulo: "Equipe", valor: (r) => r.equipe, html: (r) => r.equipe ? esc(r.equipe) : `<span class="fraco">sem equipe</span>` },
    { id: "bairro", rotulo: "Bairro", valor: (r) => r.bairro, html: (r) => `${esc(r.bairro)}${r.endereco ? `<div class="secundario celula-endereco" title="${esc(r.endereco)}">${esc(r.endereco)}</div>` : ""}`, cheio: true },
    { id: "tipo", rotulo: "Tipo", valor: (r) => r.tipo, html: (r) => esc(r.tipo), cheio: true },
  ],
  programaveis: [
    { id: "restante", rotulo: "Tempo restante", valor: (r) => r._min, html: celRestante },
    { id: "vencimento", rotulo: "Vencimento", valor: (r) => r._min, html: (r, agora) => `<span class="num">${esc(horaOuDia(r._venc, agora))}</span>` },
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
  equipes: [
    { id: "equipe", rotulo: "Equipe", valor: (r) => r.equipe, html: (r) => `<button class="link-equipe" data-equipe="${esc(r.equipe)}">${esc(r.equipe || "(sem equipe)")}</button>` },
    { id: "total", rotulo: "Em campo", valor: (r) => r.total, html: (r) => `<span class="num">${r.total}</span>` },
    { id: "vencidas", rotulo: "Vencidas", valor: (r) => r.vencidas, html: (r) => `<span class="num" style="color:${r.vencidas ? "var(--vencida)" : "var(--texto-3)"}">${r.vencidas}</span>` },
    { id: "ate60", rotulo: "Vencem ≤ 1h", valor: (r) => r.ate60, html: (r) => `<span class="num" style="color:${r.ate60 ? "var(--alerta)" : "var(--texto-3)"}">${r.ate60}</span>` },
    { id: "restante", rotulo: "Próximo vencimento", valor: (r) => r._min, html: (r) => r._min == null ? `<span class="fraco">—</span>` : celRestante(r) },
    { id: "distribuicao", rotulo: "Distribuição", valor: null, html: (r) => {
      const seg = ["vencida", "critico", "alerta", "atencao", "ok", "sem"]
        .filter((u) => r.dist[u])
        .map((u) => `<span style="width:${(r.dist[u] / r.total) * 100}%;background:var(--${u})" title="${u}: ${r.dist[u]}"></span>`)
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

function agruparPorEquipe(campo) {
  const grupos = new Map();
  for (const r of campo) {
    const k = r.equipe || "";
    if (!grupos.has(k)) grupos.set(k, { equipe: k, total: 0, vencidas: 0, ate60: 0, _min: null, dist: {} });
    const g = grupos.get(k);
    g.total++;
    if (FILTRO_URGENCIA.vencida(r._min)) g.vencidas++;
    if (FILTRO_URGENCIA.alerta(r._min)) g.ate60++;
    g.dist[r._urg] = (g.dist[r._urg] || 0) + 1;
    if (r._min != null && r._min > 0 && (g._min == null || r._min < g._min)) g._min = r._min;
  }
  // Equipe com vencida em aberto fica vermelha mesmo que a próxima ainda esteja no prazo.
  return [...grupos.values()].map((g) => ({ ...g, _urg: g.vencidas ? "vencida" : classificar(g._min) }));
}

function filtrar(linhas) {
  const termo = estado.texto.trim().toLowerCase();
  return linhas.filter((r) => {
    if (estado.aba === "campo") {
      if (estado.equipe && (r.equipe || "") !== estado.equipe) return false;
      if (estado.bairro && (r.bairro || "") !== estado.bairro) return false;
    }
    if (estado.urg && !FILTRO_URGENCIA[estado.urg](r._min)) return false;
    if (termo) {
      const alvo = [r.ordem, r.tdc, r.cliente, r.equipe, r.bairro, r.tipo, r.endereco, r.nome_cliente].join(" ").toLowerCase();
      if (!alvo.includes(termo)) return false;
    }
    return true;
  });
}

function ordenar(linhas, colunas) {
  const { col, dir } = estado.ordem[estado.aba];
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

function renderTabela(campo, programaveis, agora) {
  const colunas = COLUNAS[estado.aba];
  let linhas;
  if (estado.aba === "campo") linhas = filtrar(campo);
  else if (estado.aba === "programaveis") linhas = filtrar(programaveis);
  else if (estado.aba === "acessos") linhas = estado.acessos.map((a) => ({ ...a, _urg: a.status === "pendente" ? "alerta" : a.status === "recusado" ? "sem" : "ok" }));
  else linhas = agruparPorEquipe(campo);
  linhas = ordenar(linhas, colunas);

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
    const temDados = estado.aba === "programaveis" ? programaveis.length : campo.length;
    vazio.textContent = estado.aba === "acessos"
      ? "Nenhum pedido de acesso."
      : !estado.snapshot
      ? "Carregando…"
      : temDados ? "Nenhuma ordem com esses filtros." : estado.aba === "programaveis" ? "Nenhuma religação programável no momento." : "Nenhuma religação em campo no relatório.";
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
  estado.equipe = "";
  estado.bairro = "";
  trocarAba(aba);
});

$("#tabela-cabecalho").addEventListener("click", (ev) => {
  const th = ev.target.closest("th[data-col]");
  if (!th) return;
  const o = estado.ordem[estado.aba];
  if (o.col === th.dataset.col) o.dir *= -1;
  else { o.col = th.dataset.col; o.dir = th.dataset.col === "vencidas" || th.dataset.col === "total" || th.dataset.col === "ate60" ? -1 : 1; }
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

$("#btn-atualizar").addEventListener("click", () => buscarSnapshot());
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
// INÍCIO
// ------------------------------------------------------------------
let timers = [];
function pararTimers() {
  timers.forEach(clearInterval);
  timers = [];
}

function iniciarPainel() {
  mostrarTela("#tela-painel");
  pararTimers();
  timers = [
    setInterval(() => { if (document.visibilityState === "visible") buscarSnapshot(); }, INTERVALO_BUSCA_MS),
    setInterval(renderizar, INTERVALO_RELOGIO_MS),
  ];
  renderizar();
  buscarSnapshot();
}

async function iniciar() {
  if (prefsSalvas.tema) document.documentElement.dataset.theme = prefsSalvas.tema;
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
    $("#form-login button").disabled = true;
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
