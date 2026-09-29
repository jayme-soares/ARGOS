// Função serverless do Vercel: devolve o snapshot publicado pelo bot no
// Upstash Redis, só para usuários logados no Supabase E aprovados no ARGOS
// (tabela public.argos_acessos — ver supabase/migrations).
const KEY = process.env.ARGOS_SNAPSHOT_KEY || "argos:snapshot";

// Valida o token chamando a RPC com o próprio JWT do usuário: o PostgREST do
// Supabase rejeita token inválido/expirado (401), e a função devolve o
// status de acesso de auth.uid(). Uma chamada resolve as duas coisas.
async function acessoDoUsuario(token) {
  const r = await fetch(`${process.env.SUPABASE_URL}/rest/v1/rpc/argos_meu_acesso`, {
    method: "POST",
    headers: {
      apikey: process.env.SUPABASE_ANON_KEY,
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: "{}",
  });
  if (r.status === 401 || r.status === 403) return { invalido: true };
  if (!r.ok) throw new Error(`Supabase respondeu ${r.status}`);
  return (await r.json()) || {};
}

export default async function handler(req, res) {
  res.setHeader("Cache-Control", "no-store");

  const faltando = ["SUPABASE_URL", "SUPABASE_ANON_KEY", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"]
    .filter((n) => !process.env[n]);
  if (faltando.length) {
    return res.status(500).json({ erro: `Variáveis não configuradas no Vercel: ${faltando.join(", ")}.` });
  }

  const [tipo, token] = (req.headers.authorization || "").split(" ");
  if (tipo !== "Bearer" || !token) {
    return res.status(401).json({ erro: "Faça login." });
  }

  let acesso;
  try {
    acesso = await acessoDoUsuario(token);
  } catch (e) {
    return res.status(502).json({ erro: `Falha ao validar o acesso: ${e.message}` });
  }
  if (acesso.invalido) {
    return res.status(401).json({ erro: "Sessão expirada. Entre novamente." });
  }
  if (acesso.status !== "aprovado") {
    return res.status(403).json({ erro: "Acesso ainda não aprovado.", status: acesso.status || "sem_solicitacao" });
  }

  try {
    const r = await fetch(`${process.env.UPSTASH_REDIS_REST_URL}/get/${encodeURIComponent(KEY)}`, {
      headers: { Authorization: `Bearer ${process.env.UPSTASH_REDIS_REST_TOKEN}` },
    });
    if (!r.ok) {
      return res.status(502).json({ erro: `Upstash respondeu ${r.status}.` });
    }
    const { result } = await r.json();
    if (!result) {
      return res.status(404).json({ erro: "O bot ainda não publicou nenhum dado." });
    }
    res.setHeader("Content-Type", "application/json; charset=utf-8");
    return res.status(200).send(result);
  } catch (e) {
    return res.status(502).json({ erro: `Falha ao ler o Upstash: ${e.message}` });
  }
}
