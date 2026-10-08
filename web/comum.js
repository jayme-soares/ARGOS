"use strict";

// ARGOS — código compartilhado pelo painel da gestão (app.js) e pelo painel
// das equipes (equipe.js): datas, urgência, cliente do Supabase e Web Push.
// Script clássico carregado antes do script da página; as funções que ele
// chama de volta (renderizar) são definidas pela página.

const TZ = "America/Sao_Paulo";
const INTERVALO_BUSCA_MS = 60_000;
const INTERVALO_RELOGIO_MS = 30_000;
const INTERVALO_CONTADOR_MS = 1_000;

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

// Estado comum às duas páginas; cada página acrescenta o seu (Object.assign).
const estado = {
  snapshot: null,
  sb: null,          // cliente Supabase
  acesso: null,      // linha do usuário em argos_acessos
  ultimoErro: null,
  push: null,        // estado do sino — ver atualizarEstadoPush()
  pushSincronizado: false,
};

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

// Finalizadas: no prazo / fora do prazo, com a folga em relação ao vencimento.
function textoSituacaoFinalizada(r) {
  if (r.no_prazo === true) return r._folga != null && r._folga >= 1 ? `no prazo · ${duracao(r._folga)} antes` : "no prazo";
  if (r.no_prazo === false) return `fora do prazo · ${duracao(r._folga ?? 0)} depois`;
  return "sem prazo";
}

function descreverStatus(st) {
  const e = st?.estado || "sem dados";
  if (e === "ok") return { cls: "ok", txt: "ok" };
  if (e === "fora_do_horario") return { cls: "pausa", txt: "fora do horário" };
  if (e.startsWith("erro")) return { cls: "erro", txt: "erro" };
  return { cls: "ocupado", txt: e.replaceAll("_", " ") };
}

// ------------------------------------------------------------------
// BLOCOS DO PAINEL
// ------------------------------------------------------------------
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

// ------------------------------------------------------------------
// SUPABASE
// ------------------------------------------------------------------
// Lê /api/config e cria o cliente. O supabase-js guarda a sessão no
// localStorage e renova o token sozinho. Devolve a configuração pública.
async function criarSupabase() {
  const r = await fetch("/api/config");
  const cfg = await r.json();
  if (!r.ok) throw new Error(cfg.erro || `Erro ${r.status}`);
  estado.sb = window.supabase.createClient(cfg.supabaseUrl, cfg.supabaseAnonKey, {
    auth: { persistSession: true, autoRefreshToken: true, storageKey: "argos.auth" },
  });
  return cfg;
}

async function tokenAtual() {
  const { data } = await estado.sb.auth.getSession();
  return data.session?.access_token || null;
}

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

// Anda os cronômetros ([data-contagem-ate]) a cada segundo sem redesenhar
// a página inteira; ao zerar, redesenha para pegar a próxima ordem.
function atualizarContadores() {
  const agora = Date.now();
  for (const el of $$("[data-contagem-ate]")) {
    const restante = Number(el.dataset.contagemAte) - agora;
    if (restante <= 0) { renderizar(); return; }
    el.textContent = cronometro(restante);
  }
}
