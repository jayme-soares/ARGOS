// Função serverless do Vercel: snapshot para o modo TV (/tv), sem login.
// A TV da sala de reuniões entra com uma chave fixa (variável ARGOS_TV_CHAVE
// no Vercel; várias separadas por vírgula, para trocar sem derrubar a TV).
// Trocar ou apagar a variável revoga o acesso.
//
// Só vai o que a tela mostra: sem nome, endereço nem código do cliente.
import { timingSafeEqual } from "node:crypto";

const KEY = process.env.ARGOS_SNAPSHOT_KEY || "argos:snapshot";
// Iguais a Contrato.snapshot_key em argos/contratos.py.
const CHAVES = { soc: KEY, negociacao: `${KEY}:negociacao` };

function chaveValida(recebida) {
  const aceitas = (process.env.ARGOS_TV_CHAVE || "").split(",").map((c) => c.trim()).filter(Boolean);
  const b = Buffer.from(String(recebida || ""));
  return b.length > 0 && aceitas.some((c) => {
    const a = Buffer.from(c);
    return a.length === b.length && timingSafeEqual(a, b);
  });
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

export default async function handler(req, res) {
  res.setHeader("Cache-Control", "no-store");

  const faltando = ["ARGOS_TV_CHAVE", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"].filter((n) => !process.env[n]);
  if (faltando.length) {
    return res.status(500).json({ erro: `Variáveis não configuradas no Vercel: ${faltando.join(", ")}.` });
  }
  if (!chaveValida(req.headers["x-argos-tv"])) {
    return res.status(401).json({ erro: "Chave da TV inválida." });
  }
  const contrato = String(req.query?.contrato || "soc");
  if (!CHAVES[contrato]) {
    return res.status(400).json({ erro: "Contrato inválido." });
  }

  try {
    const r = await fetch(`${process.env.UPSTASH_REDIS_REST_URL}/get/${encodeURIComponent(CHAVES[contrato])}`, {
      headers: { Authorization: `Bearer ${process.env.UPSTASH_REDIS_REST_TOKEN}` },
    });
    if (!r.ok) {
      return res.status(502).json({ erro: `Upstash respondeu ${r.status}.` });
    }
    const { result } = await r.json();
    if (!result) {
      return res.status(404).json({ erro: "O bot ainda não publicou nenhum dado." });
    }
    return res.status(200).json(snapshotDaTv(JSON.parse(result)));
  } catch (e) {
    return res.status(502).json({ erro: `Falha ao ler o Upstash: ${e.message}` });
  }
}
