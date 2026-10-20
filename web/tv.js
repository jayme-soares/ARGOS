"use strict";

// ARGOS — modo TV (/tv). Tela fixa para a sala de reuniões: lê o mesmo
// snapshot do painel (pelo /api/tv) a cada 60s, recalcula as cores a cada
// segundo e passa as páginas da lista sozinha.
//
// Pareamento por PIN, sem digitar nada na TV: na primeira vez a tela mostra
// um código de 6 dígitos; um admin digita o código no painel (aba TVs) e
// escolhe o contrato, o tema, o zoom e os COs. A TV guarda o token dela no
// navegador e segue pareada até ser removida no painel.
//
// Escrito sem ?., ??, replaceAll, Object.fromEntries e afins: o navegador
// das TVs Samsung mais antigas é um Chromium de 2019.

var TZ = "America/Sao_Paulo";
var INTERVALO_BUSCA_MS = 60000;
var INTERVALO_PAREAMENTO_MS = 3000;
var INTERVALO_PAGINA_MS = 15000;
var RECARREGAR_MS = 3 * 3600000;  // pega versões novas do painel e limpa a memória
var CHAVE_TOKEN = "argos.tv.token";
var NOMES_CONTRATO = { soc: "SOC · Maricá", negociacao: "Negociação" };

// Definidos pelo admin no painel (vêm com o snapshot).
var CONTRATO = "soc";
var FILTRO_CO = [];

function lerToken() { try { return localStorage.getItem(CHAVE_TOKEN); } catch (e) { return null; } }
function gravarToken(t) {
  try { if (t) localStorage.setItem(CHAVE_TOKEN, t); else localStorage.removeItem(CHAVE_TOKEN); } catch (e) { /* fica só na memória */ }
}

var estado = {
  token: lerToken(),
  pareada: false,
  pin: null,
  pinExpira: null,
  tv: null,             // nome, contrato e config definidos no painel
  snapshot: null,
  ultimoErro: null,
  ultimaLeitura: null,  // quando a TV leu o snapshot pela última vez
  offset: 0,            // relógio do servidor - relógio da TV (a TV pode estar errada)
  pagina: 0,
  porPagina: 10,
  equipesPorPagina: 8,
};

function $(sel) { return document.querySelector(sel); }
function agoraCorrigido() { return new Date(Date.now() + estado.offset); }

// ------------------------------------------------------------------
// DATAS / FORMATAÇÃO
// ------------------------------------------------------------------
var fmtHora = new Intl.DateTimeFormat("pt-BR", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hour12: false });
var fmtDiaHora = new Intl.DateTimeFormat("pt-BR", { timeZone: TZ, day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
var fmtDataLonga = new Intl.DateTimeFormat("pt-BR", { timeZone: TZ, weekday: "long", day: "2-digit", month: "long" });
var fmtDia = new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" });
var fmtPartes = new Intl.DateTimeFormat("en-US", { timeZone: TZ, weekday: "short", hour: "numeric", hour12: false });

function data(iso) {
  if (!iso) return null;
  var d = new Date(iso);
  return isNaN(d) ? null : d;
}
function mesmoDia(a, b) { return fmtDia.format(a) === fmtDia.format(b); }
function diaHora(d) { return d ? fmtDiaHora.format(d).replace(",", "") : "—"; }
function dois(n) { return (n < 10 ? "0" : "") + n; }
function cronometro(ms) {
  var total = Math.max(0, Math.floor(ms / 1000));
  return [Math.floor(total / 3600), Math.floor((total % 3600) / 60), total % 60].map(dois).join(":");
}
function duracao(min) {
  var total = Math.round(Math.abs(min));
  var d = Math.floor(total / 1440);
  var h = Math.floor((total % 1440) / 60);
  var m = total % 60;
  if (d) return h ? d + "d " + h + "h" : d + "d";
  if (h) return m ? h + "h" + dois(m) : h + "h";
  return m + " min";
}
function textoRestante(min) {
  if (min == null) return "sem prazo";
  return min <= 0 ? "vencida há " + duracao(min) : "em " + duracao(min);
}
function haQuanto(d, agora) {
  if (!d) return "nunca";
  var min = (agora - d) / 60000;
  return min < 1 ? "agora" : "há " + duracao(min);
}
function esc(v) {
  return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}

var DIAS_SEMANA = { Mon: 0, Tue: 1, Wed: 2, Thu: 3, Fri: 4, Sat: 5, Sun: 6 };
function dentroDaJanela(agora, janela) {
  if (!janela) return true;
  var dia = null, hora = null;
  fmtPartes.formatToParts(agora).forEach(function (p) {
    if (p.type === "weekday") dia = DIAS_SEMANA[p.value];
    if (p.type === "hour") hora = Number(p.value) % 24;
  });
  return janela.dias_semana.indexOf(dia) >= 0 && hora >= janela.inicio && hora < janela.fim;
}

// ------------------------------------------------------------------
// URGÊNCIA (igual a comum.js)
// ------------------------------------------------------------------
function classificar(min) {
  if (min == null) return "sem";
  if (min <= 0) return "vencida";
  if (min <= 30) return "critico";
  if (min <= 60) return "alerta";
  if (min <= 120) return "atencao";
  return "ok";
}
function tramiteDoCiclo(tipo) {
  var partes = String(tipo || "").split(" - ");
  var fim = partes.length > 2 ? partes[partes.length - 1].trim() : "";
  if (/ramal/i.test(fim)) return "Ramal";
  return /medidor|poste/i.test(fim) ? fim : "";
}
function enriquecer(registros, agora) {
  return (registros || []).map(function (r) {
    var venc = data(r.vencimento);
    var min = venc ? (venc - agora) / 60000 : null;
    return Object.assign({}, r, { _venc: venc, _min: min, _urg: classificar(min), _tramite: tramiteDoCiclo(r.tipo) });
  });
}
function descreverStatus(st) {
  var e = (st && st.estado) || "sem dados";
  if (e === "ok") return { cls: "ok", txt: "ok" };
  if (e === "fora_do_horario") return { cls: "pausa", txt: "fora do horário" };
  if (e.indexOf("erro parcial") === 0) return { cls: "parcial", txt: "parcial" };
  if (e.indexOf("erro") === 0) return { cls: "erro", txt: "erro" };
  return { cls: "ocupado", txt: e.split("_").join(" ") };
}
// "Ceneged - Angra (CENEGED_ANGRA)" -> "Angra" (igual a app.js).
function nomeCo(co) { return String(co || "").replace(/^\s*ceneged\s*-\s*/i, "").replace(/\s*\([^)]*\)?\s*$/, ""); }
function nomeEquipe(r) { return r.equipe_nome || r.equipe || ""; }
function noFiltroCo(r) {
  if (!FILTRO_CO.length) return true;
  var completo = String(r.co || "").toLowerCase();
  var curto = nomeCo(r.co).toLowerCase();
  return FILTRO_CO.indexOf(completo) >= 0 || FILTRO_CO.indexOf(curto) >= 0;
}

// ------------------------------------------------------------------
// API
// ------------------------------------------------------------------
async function chamarApi(metodo) {
  var r = await fetch("/api/tv", {
    method: metodo, headers: estado.token ? { "X-Argos-Tv": estado.token } : {}, cache: "no-store",
  });
  var servidor = data(r.headers.get("date"));
  if (servidor) estado.offset = servidor - Date.now();
  var corpo = {};
  try { corpo = await r.json(); } catch (e) { corpo = {}; }
  return { status: r.status, ok: r.ok, corpo: corpo };
}

// Tela do PIN (TV ainda não pareada, ou removida no painel).
function mostrarPin(texto) {
  estado.pareada = false;
  $("#tela").hidden = true;
  $("#pareamento").hidden = false;
  $("#pin").textContent = estado.pin ? estado.pin.slice(0, 3) + " " + estado.pin.slice(3) : "··· ···";
  $("#pin-erro").textContent = texto || "";
  $("#pin-endereco").textContent = location.host;
  renderValidadePin();
}
function renderValidadePin() {
  var el = $("#pin-validade");
  if (!estado.pinExpira) { el.textContent = ""; return; }
  var resta = estado.pinExpira - agoraCorrigido();
  el.textContent = resta > 0 ? "Código válido por " + cronometro(resta).slice(3) : "Gerando um código novo…";
}

async function novoPin() {
  estado.token = null;
  gravarToken(null);
  var r = await chamarApi("POST");
  if (!r.ok) throw new Error(r.corpo.erro || "Erro " + r.status);
  estado.token = r.corpo.token;
  gravarToken(estado.token);
  estado.pin = r.corpo.pin;
  estado.pinExpira = data(r.corpo.expira_em);
  mostrarPin();
}

function aplicarConfig(tv) {
  var cfg = (tv && tv.config) || {};
  CONTRATO = NOMES_CONTRATO[tv && tv.contrato] ? tv.contrato : "soc";
  FILTRO_CO = (cfg.cos || []).map(function (c) { return String(c).trim().toLowerCase(); }).filter(Boolean);
  if (cfg.tema === "claro") document.documentElement.setAttribute("data-theme", "light");
  else document.documentElement.setAttribute("data-theme", "dark");
  document.documentElement.style.fontSize = (0.9 * (Number(cfg.zoom) || 1)) + "vw";
}

// Uma volta: pede o PIN, espera o pareamento ou lê o snapshot. Devolve
// quanto esperar até a próxima volta.
async function ciclo() {
  try {
    if (!estado.token) { await novoPin(); return INTERVALO_PAREAMENTO_MS; }
    var r = await chamarApi("GET");
    if (r.status === 401) { await novoPin(); return INTERVALO_PAREAMENTO_MS; }  // removida no painel, ou PIN expirou
    if (!r.ok) throw new Error(r.corpo.erro || "Erro " + r.status);
    if (r.corpo.status === "pendente") {
      estado.pin = r.corpo.pin;
      estado.pinExpira = data(r.corpo.expira_em);
      mostrarPin();
      return INTERVALO_PAREAMENTO_MS;
    }
    estado.tv = r.corpo.tv;
    aplicarConfig(estado.tv);
    estado.snapshot = r.corpo.snapshot;
    estado.ultimoErro = r.corpo.snapshot ? null : "o bot ainda não publicou nenhum dado";
    estado.ultimaLeitura = agoraCorrigido();
    estado.pareada = true;
    $("#pareamento").hidden = true;
    $("#tela").hidden = false;
    renderizar();
    return INTERVALO_BUSCA_MS;
  } catch (e) {
    estado.ultimoErro = (e && e.message) || String(e);
    if (!estado.pareada) { mostrarPin("Sem conexão com o ARGOS (" + estado.ultimoErro + "). Tentando de novo…"); return 10000; }
    renderizar();
    return 15000;
  }
}

// Uma volta por vez: chamar de novo (ex.: a TV voltou do standby) adianta a próxima.
var proximaVolta = null;
var emVolta = false;
async function loop() {
  clearTimeout(proximaVolta);
  if (emVolta) return;
  emVolta = true;
  var espera = await ciclo();
  emVolta = false;
  proximaVolta = setTimeout(loop, espera);
}

// ------------------------------------------------------------------
// RENDERIZAÇÃO
// ------------------------------------------------------------------
function dados(agora) {
  var snap = estado.snapshot || {};
  var campoSnap = snap.campo || {};
  var progSnap = snap.programaveis || {};
  var campo = enriquecer(campoSnap.registros, agora).filter(noFiltroCo);
  var programaveis = enriquecer(progSnap.registros, agora).filter(noFiltroCo);
  var finalizadas = (campoSnap.finalizadas || []).filter(noFiltroCo).filter(function (r) {
    var fim = data(r.finalizada_em);
    return fim && mesmoDia(fim, agora);
  });
  var totalProg = progSnap.total != null ? progSnap.total : programaveis.length;
  if (FILTRO_CO.length) {
    var totais = progSnap.total_por_co || {};
    var cos = {};
    programaveis.forEach(function (r) { cos[r.co || ""] = true; });
    Object.keys(totais).forEach(function (co) { if (noFiltroCo({ co: co })) cos[co] = true; });
    totalProg = Object.keys(cos).reduce(function (s, co) {
      return s + (totais[co] != null ? totais[co] : programaveis.filter(function (r) { return r.co === co; }).length);
    }, 0);
  }
  return { snap: snap, campo: campo, programaveis: programaveis, finalizadas: finalizadas, totalProg: totalProg };
}

function kpi(rotulo, valor, detalhe, cor, piscar) {
  var estilo = cor ? ' style="--cor: var(--' + cor + ')"' : "";
  return '<div class="kpi' + (valor === 0 ? " zero" : "") + (piscar ? " piscando" : "") + '"' + estilo + ">"
    + '<div class="rotulo">' + esc(rotulo) + "</div>"
    + '<div class="valor">' + esc(valor) + "</div>"
    + '<div class="detalhe">' + esc(detalhe) + "</div></div>";
}

function renderKpis(d, agora) {
  var campo = d.campo;
  function conta(f) { return campo.filter(function (r) { return r._min != null && f(r._min); }).length; }
  var vencidas = conta(function (m) { return m <= 0; });
  var ate30 = conta(function (m) { return m > 0 && m <= 30; });
  var ate60 = conta(function (m) { return m > 0 && m <= 60; });
  var hoje = campo.filter(function (r) { return r._min > 0 && mesmoDia(r._venc, agora); }).length;
  var noPrazo = d.finalizadas.filter(function (r) { return r.no_prazo === true; }).length;
  var fora = d.finalizadas.filter(function (r) { return r.no_prazo === false; }).length;
  var novasProg = d.programaveis.filter(function (r) {
    var p = data(r.primeiro_visto_em);
    return p && agora - p <= 3600000;
  }).length;
  $("#kpis").innerHTML = [
    kpi("Em campo", campo.length, "em aberto no relatório"),
    kpi("Vencidas", vencidas, "em aberto", vencidas ? "vencida" : null, vencidas > 0),
    kpi("Vencem ≤ 30 min", ate30, "prioridade máxima", ate30 ? "critico" : null),
    kpi("Vencem ≤ 1h", ate60, "inclui as de ≤ 30 min", ate60 ? "alerta" : null),
    kpi("Vencem hoje", hoje, "ainda no prazo", null),
    kpi("Finalizadas hoje", d.finalizadas.length, d.finalizadas.length ? noPrazo + " no prazo · " + fora + " fora" : "nenhuma ainda", d.finalizadas.length ? "ok" : null),
    kpi("Programáveis", d.totalProg, novasProg ? novasProg + " nova(s) em 1h" : "aguardando designação", d.totalProg ? "atencao" : null),
  ].join("");
}

function renderStatus(snap) {
  var pills = [["programaveis", "Programáveis"], ["campo", "Em campo"]].map(function (p) {
    var st = descreverStatus(snap.status && snap.status[p[0]]);
    var sec = snap[p[0]] || {};
    var atualizado = data(sec.atualizado_em);
    return '<span class="pill"><span class="ponto ' + st.cls + '"></span><b>' + p[1] + "</b>"
      + esc(st.txt) + (atualizado ? " · " + fmtHora.format(atualizado) : "") + "</span>";
  });
  $("#status").innerHTML = estado.snapshot ? pills.join("") : "";
}

function renderAvisos(snap, agora) {
  var avisos = [];
  if (estado.ultimoErro) {
    avisos.push({ cls: "erro", txt: "Sem conexão com o ARGOS (" + estado.ultimoErro + ")."
      + (estado.ultimaLeitura ? " Exibindo os dados de " + fmtHora.format(estado.ultimaLeitura) + "." : "") });
  }
  if (estado.snapshot) {
    var naJanela = dentroDaJanela(agora, snap.janela);
    if (!naJanela && snap.janela) avisos.push({ cls: "info", txt: "Fora do horário de funcionamento do bot — retoma às " + snap.janela.inicio + "h." });
    var gerado = data(snap.gerado_em);
    if (naJanela && gerado && agora - gerado > 10 * 60000) avisos.push({ cls: "erro", txt: "O bot não publica nada " + haQuanto(gerado, agora) + "." });
    var intervalo = (snap.config && snap.config.intervalo_campo_min) || 30;
    var campoEm = data(snap.campo && snap.campo.atualizado_em);
    if (naJanela && (!campoEm || agora - campoEm > (intervalo + 15) * 60000)) {
      avisos.push({ txt: "Dados de campo desatualizados (última exportação " + haQuanto(campoEm, agora) + ")." });
    }
    var progEm = data(snap.programaveis && snap.programaveis.atualizado_em);
    if (naJanela && (!progEm || agora - progEm > 5 * 60000)) {
      avisos.push({ txt: "Programáveis desatualizadas (última checagem " + haQuanto(progEm, agora) + ")." });
    }
    [["programaveis", "Programáveis"], ["campo", "Em campo"]].forEach(function (p) {
      var e = (snap.status && snap.status[p[0]] && snap.status[p[0]].estado) || "";
      if (e.indexOf("erro") === 0 && e.indexOf("erro parcial") !== 0) avisos.push({ cls: "erro", txt: p[1] + ": erro na última leitura do eOrder." });
      var cos = (snap[p[0]] && snap[p[0]].cos_com_erro) || [];
      if (cos.length) avisos.push({ txt: p[1] + ": sem dados novos de " + cos.map(nomeCo).join(", ") + " (ficaram os anteriores)." });
    });
  }
  $("#avisos").innerHTML = avisos.map(function (a) {
    return '<div class="aviso' + (a.cls ? " " + a.cls : "") + '">' + esc(a.txt) + "</div>";
  }).join("");
}

// Vencidas primeiro (a mais atrasada no topo), depois as que vencem antes;
// sem prazo no fim.
function ordenarPorVencimento(linhas) {
  return linhas.slice().sort(function (a, b) {
    if (a._min == null || b._min == null) return a._min == null ? (b._min == null ? 0 : 1) : -1;
    return a._min - b._min;
  });
}

function colunas() {
  var cols = [
    ["Tempo restante", "17%", function (r) { return '<span class="badge">' + esc(textoRestante(r._min)) + "</span>"; }],
    ["Vencimento", "12%", function (r) { return '<span class="num">' + esc(diaHora(r._venc)) + "</span>"; }],
  ];
  if (CONTRATO === "negociacao") cols.push(["CO", "9%", function (r) { return esc(nomeCo(r.co)); }]);
  return cols.concat([
    ["Equipe", "21%", function (r) { return r.equipe ? esc(nomeEquipe(r)) : '<span class="fraco">sem equipe</span>'; }],
    ["Bairro", "15%", function (r) { return esc(r.bairro); }],
    ["Trâmite", "11%", function (r) { return r._tramite ? esc(r._tramite) : '<span class="fraco">—</span>'; }],
    ["TdC", "11%", function (r) { return '<span class="num secundario">' + esc(r.tdc) + "</span>"; }],
  ]);
}

// Quantas linhas cabem na área da lista (a altura muda com a TV e com os avisos).
function medirLinhas() {
  var area = $("#lista-area");
  var cab = $("#tabela-cabecalho");
  var linha = $("#tabela-corpo tr");
  var altura = linha ? linha.getBoundingClientRect().height : 2.75 * parseFloat(getComputedStyle(document.documentElement).fontSize);
  var livre = area.clientHeight - (cab ? cab.getBoundingClientRect().height : 0);
  estado.porPagina = Math.max(3, Math.floor(livre / altura));

  var eqArea = $("#equipes-area");
  var eq = $("#equipes .eq:not(.cabecalho)");
  var eqAltura = eq ? eq.getBoundingClientRect().height : 2.6 * parseFloat(getComputedStyle(document.documentElement).fontSize);
  estado.equipesPorPagina = Math.max(2, Math.floor((eqArea.clientHeight - eqAltura * 0.7) / eqAltura));
}

function renderLista(d) {
  var cols = colunas();
  var linhas = ordenarPorVencimento(d.campo);
  var paginas = Math.max(1, Math.ceil(linhas.length / estado.porPagina));
  if (estado.pagina >= paginas) estado.pagina = 0;
  var inicio = estado.pagina * estado.porPagina;
  var visiveis = linhas.slice(inicio, inicio + estado.porPagina);

  $("#tabela-cabecalho").innerHTML = "<tr>" + cols.map(function (c) {
    return '<th style="width:' + c[1] + '">' + esc(c[0]) + "</th>";
  }).join("") + "</tr>";
  $("#tabela-corpo").innerHTML = visiveis.map(function (r) {
    return '<tr class="u-' + r._urg + '">' + cols.map(function (c) { return "<td>" + c[2](r) + "</td>"; }).join("") + "</tr>";
  }).join("");

  var vazio = $("#vazio");
  vazio.hidden = linhas.length > 0;
  vazio.textContent = estado.snapshot ? "Nenhuma religação em campo no relatório." : "Carregando…";
  $("#pagina").textContent = linhas.length
    ? (paginas > 1 ? "página " + (estado.pagina + 1) + "/" + paginas + " · " : "") + linhas.length + " ordem(ns)"
    : "";
}

function renderProximo(d, agora) {
  var proxima = d.campo.filter(function (r) { return r._min > 0; }).sort(function (a, b) { return a._min - b._min; })[0];
  var el = $("#proximo");
  el.style.setProperty("--cor", proxima ? "var(--" + proxima._urg + ")" : "var(--borda)");
  el.innerHTML = '<div class="proximo-rotulo">Próximo vencimento em</div>'
    + '<div class="proximo-valor" ' + (proxima ? 'data-contagem-ate="' + proxima._venc.getTime() + '"' : "") + ">"
    + (proxima ? cronometro(proxima._venc - agora) : "—") + "</div>"
    + '<div class="proximo-detalhe">' + esc(proxima
      ? "vence " + diaHora(proxima._venc) + " · TdC " + proxima.tdc + " · " + (nomeEquipe(proxima) || "sem equipe")
      : "nenhuma ordem no prazo") + "</div>";
}

function renderEquipes(d) {
  var grupos = {};
  d.campo.forEach(function (r) {
    var k = r.equipe || "";
    var g = grupos[k] || (grupos[k] = { nome: nomeEquipe(r) || "sem equipe", total: 0, vencidas: 0, min: null });
    g.total++;
    if (r._min != null && r._min <= 0) g.vencidas++;
    else if (r._min != null && (g.min == null || r._min < g.min)) g.min = r._min;
  });
  var lista = Object.keys(grupos).map(function (k) { return grupos[k]; }).sort(function (a, b) {
    if (a.vencidas !== b.vencidas) return b.vencidas - a.vencidas;
    if ((a.min == null) !== (b.min == null)) return a.min == null ? 1 : -1;
    if (a.min !== b.min) return a.min - b.min;
    return b.total - a.total;
  });
  var mostra = lista.slice(0, estado.equipesPorPagina);
  $("#equipes").innerHTML = (lista.length
    ? '<div class="eq cabecalho"><span class="eq-nome">Equipe</span><span class="eq-num">Abertas</span><span class="eq-num">Venc.</span><span class="eq-prox">Próxima</span></div>'
    : '<p class="vazio">Nenhuma equipe com ordens em aberto.</p>')
    + mostra.map(function (g) {
      var urg = g.vencidas ? "vencida" : classificar(g.min);
      return '<div class="eq u-' + urg + '"><span class="eq-nome">' + esc(g.nome) + "</span>"
        + '<span class="eq-num">' + g.total + "</span>"
        + '<span class="eq-num' + (g.vencidas ? " vencidas" : "") + '">' + g.vencidas + "</span>"
        + '<span class="eq-prox">' + (g.min != null ? esc(duracao(g.min)) : "—") + "</span></div>";
    }).join("");
  $("#equipes-extra").textContent = lista.length > mostra.length ? "+" + (lista.length - mostra.length) + " equipe(s)" : (lista.length ? lista.length + " equipe(s)" : "");
}

function renderProgramaveis(d) {
  var lista = ordenarPorVencimento(d.programaveis).slice(0, 4);
  $("#prog-total").textContent = d.totalProg ? d.totalProg + " no total" : "";
  $("#programaveis").innerHTML = lista.length
    ? lista.map(function (r) {
      return '<div class="prog u-' + r._urg + '"><span class="badge">' + esc(textoRestante(r._min)) + "</span>"
        + '<span class="prog-tdc">TdC ' + esc(r.tdc) + "</span>"
        + '<span class="prog-venc">' + esc(diaHora(r._venc)) + "</span></div>";
    }).join("")
    : '<p class="fraco" style="margin:.3rem 0 .2rem;font-size:1.1rem">Nenhuma religação programável no momento.</p>';
}

function renderRodape(snap, agora) {
  var ultima = data(snap.campo && snap.campo.atualizado_em);
  var proxima = data(snap.campo && snap.campo.proxima_extracao);
  var partes = [];
  if (estado.snapshot) {
    partes.push("Última extração: " + diaHora(ultima) + (ultima ? " (" + haQuanto(ultima, agora) + ")" : ""));
    if (proxima && proxima > agora && dentroDaJanela(agora, snap.janela)) partes.push("Próxima: " + fmtHora.format(proxima));
  }
  if (FILTRO_CO.length) partes.push("COs: " + FILTRO_CO.join(", "));
  var direita = estado.ultimaLeitura ? "Atualizado " + haQuanto(estado.ultimaLeitura, agora) + " · atualiza a cada 60s" : "";
  $("#rodape").innerHTML = "<div>" + partes.map(function (p) { return "<span>" + esc(p) + "</span>"; }).join("") + "</div><div>" + esc(direita) + "</div>";
}

function renderRelogio(agora) {
  $("#relogio-hora").textContent = fmtHora.format(agora);
  var dia = fmtDataLonga.format(agora);
  $("#relogio-data").textContent = dia.charAt(0).toUpperCase() + dia.slice(1);
}

function renderizar() {
  var agora = agoraCorrigido();
  var d = dados(agora);
  var nome = NOMES_CONTRATO[CONTRATO];
  $("#subtitulo").textContent = "Religações · " + nome + (estado.tv && estado.tv.nome ? " · " + estado.tv.nome : "");
  document.title = "ARGOS · TV · " + nome;
  renderRelogio(agora);
  renderStatus(d.snap);
  renderAvisos(d.snap, agora);
  renderKpis(d, agora);
  renderProximo(d, agora);
  renderLista(d);
  renderEquipes(d);
  renderProgramaveis(d);
  renderRodape(d.snap, agora);
  // Depois de desenhar, confere se a quantidade de linhas ainda cabe.
  var antes = estado.porPagina, antesEq = estado.equipesPorPagina;
  medirLinhas();
  if (antes !== estado.porPagina) renderLista(d);
  if (antesEq !== estado.equipesPorPagina) renderEquipes(d);
}

// A cada segundo: relógio e contagem regressiva. Ao zerar, redesenha tudo.
function tique() {
  if (!estado.pareada) { renderValidadePin(); return; }
  var agora = agoraCorrigido();
  renderRelogio(agora);
  var els = document.querySelectorAll("[data-contagem-ate]");
  for (var i = 0; i < els.length; i++) {
    var restante = Number(els[i].getAttribute("data-contagem-ate")) - agora.getTime();
    if (restante <= 0) { renderizar(); return; }
    els[i].textContent = cronometro(restante);
  }
}

// Mantém a tela acesa onde o navegador deixar (Chromium 84+).
async function manterAcesa() {
  try { if (navigator.wakeLock) await navigator.wakeLock.request("screen"); } catch (e) { /* sem suporte */ }
}

function iniciar() {
  setInterval(tique, 1000);
  setInterval(function () { if (estado.pareada) { estado.pagina++; renderizar(); } }, INTERVALO_PAGINA_MS);
  // Recarrega a página de tempos em tempos, só se o servidor estiver
  // respondendo e o token tiver ficado guardado (senão a TV perderia o pareamento).
  setTimeout(function recarregar() {
    if (!estado.ultimoErro && estado.pareada && lerToken() === estado.token) location.reload();
    else setTimeout(recarregar, 60000);
  }, RECARREGAR_MS);
  window.addEventListener("resize", function () { if (estado.pareada) renderizar(); });
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") { manterAcesa(); loop(); }
  });
  manterAcesa();
  loop();
}

iniciar();
