// Função serverless do Vercel: modo TV (/tv), sem login, pareado por PIN
// (tabela public.argos_tvs — ver supabase/migrations/005_argos_tvs.sql).
//
// POST /api/tv                  nova TV: gera o token dela e devolve o PIN
//                               que o admin digita no painel (aba TVs).
// GET  /api/tv + X-Argos-Tv     situação da TV: pendente (com o PIN) ou
//                               aprovada (com o snapshot do contrato dela).
//                               401 = token desconhecido: a TV pede outro PIN.
//
// O token fica no navegador da TV; o banco guarda só o hash. Só vai o que a
// tela mostra: sem nome, endereço nem código do cliente.
import { createHash, randomBytes } from "node:crypto";

const KEY = process.env.ARGOS_SNAPSHOT_KEY || "argos:snapshot";
// Iguais a Contrato.snapshot_key em argos/contratos.py.
const CHAVES = { soc: KEY, negociacao: `${KEY}:negociacao` };

const hash = (token) => createHash("sha256").update(token).digest("hex");

// RPC com a anon key: as funções argos_tv_* validam pelo hash do token.
async function rpc(funcao, params) {
  const r = await fetch(`${process.env.SUPABASE_URL}/rest/v1/rpc/${funcao}`, {
    method: "POST",
    headers: {
      apikey: process.env.SUPABASE_ANON_KEY,
      Authorization: `Bearer ${process.env.SUPABASE_ANON_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(params),
  });
  const dados = await r.json().catch(() => null);
  if (!r.ok) throw new Error((dados && dados.message) || `Supabase respondeu ${r.status}`);
  return dados;
}

const escolher = (r, campos) => Object.fromEntries(campos.filter((c) => r[c] !== undefined).map((c) => [c, r[c]]));
const CAMPOS_ABERTA = ["tdc", "ordem", "equipe", "equipe_nome", "co", "bairro", "tipo", "tipo_servico", "vencimento"];
const CAMPOS_FINALIZADA = ["tdc", "equipe", "equipe_nome", "co", "vencimento", "finalizada_em", "no_prazo"];
const CAMPOS_PROGRAMAVEL = ["tdc", "ordem", "tipo", "vencimento", "primeiro_visto_em", "co"];

function snapshotDaTv(s) {
  const campo = s.campo || {};
  const prog = s.programaveis || {};
  return {
    versao: s.versao,
    gerado_em: s.gerado_em,
    contrato: s.contrato,
    janela: s.janela,
    status: s.status,
    config: { intervalo_campo_min: s.config?.intervalo_campo_min },
    campo: {
      atualizado_em: campo.atualizado_em,
      proxima_extracao: campo.proxima_extracao,
      cos_com_erro: campo.cos_com_erro || [],
      registros: (campo.registros || []).map((r) => escolher(r, CAMPOS_ABERTA)),
      finalizadas: (campo.finalizadas || []).map((r) => escolher(r, CAMPOS_FINALIZADA)),
    },
    programaveis: {
      atualizado_em: prog.atualizado_em,
      total: prog.total,
      total_por_co: prog.total_por_co,
      cos_com_erro: prog.cos_com_erro || [],
      registros: (prog.registros || []).map((r) => escolher(r, CAMPOS_PROGRAMAVEL)),
    },
  };
}

async function lerSnapshot(contrato) {
  const r = await fetch(`${process.env.UPSTASH_REDIS_REST_URL}/get/${encodeURIComponent(CHAVES[contrato])}`, {
    headers: { Authorization: `Bearer ${process.env.UPSTASH_REDIS_REST_TOKEN}` },
  });
  if (!r.ok) throw new Error(`Upstash respondeu ${r.status}.`);
  const { result } = await r.json();
  return result ? snapshotDaTv(JSON.parse(result)) : null;
}

export default async function handler(req, res) {
  res.setHeader("Cache-Control", "no-store");

  const faltando = ["SUPABASE_URL", "SUPABASE_ANON_KEY", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"]
    .filter((n) => !process.env[n]);
  if (faltando.length) {
    return res.status(500).json({ erro: `Variáveis não configuradas no Vercel: ${faltando.join(", ")}.` });
  }

  try {
    if (req.method === "POST") {
      const token = randomBytes(32).toString("hex");
      const p = await rpc("argos_tv_iniciar", { p_token_hash: hash(token), p_user_agent: req.headers["user-agent"] || null });
      return res.status(200).json({ token, pin: p.pin, expira_em: p.expira_em });
    }

    const token = String(req.headers["x-argos-tv"] || "");
    if (!/^[0-9a-f]{64}$/.test(token)) {
      return res.status(401).json({ erro: "TV não pareada." });
    }
    const tv = await rpc("argos_tv_estado", { p_token_hash: hash(token) });
    if (!tv) {
      return res.status(401).json({ erro: "TV não pareada." });
    }
    if (tv.status === "pendente") {
      return res.status(200).json({ status: "pendente", pin: tv.pin, expira_em: tv.expira_em });
    }
    if (!CHAVES[tv.contrato]) {
      return res.status(400).json({ erro: "Contrato inválido." });
    }
    const snapshot = await lerSnapshot(tv.contrato);
    return res.status(200).json({
      status: "aprovado",
      tv: { nome: tv.nome, contrato: tv.contrato, config: tv.config || {} },
      snapshot,
    });
  } catch (e) {
    return res.status(502).json({ erro: e.message });
  }
}
