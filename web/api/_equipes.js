// Utilitários das contas de equipe, compartilhados pelas funções /api/*.
// (Arquivos com "_" na frente não viram rota no Vercel.)

// As equipes entram com o código (NI2...) e a senha; o Supabase exige
// e-mail, então cada conta usa um e-mail sintético neste domínio.
export const DOMINIO_EQUIPES = process.env.ARGOS_DOMINIO_EQUIPES || "equipes.argos.local";

export const normalizarCodigo = (codigo) => String(codigo || "").replace(/\s+/g, "").toUpperCase();

export const codigoValido = (codigo) => /^NI2[A-Z0-9-]{2,30}$/.test(codigo);

export const emailDaEquipe = (codigo) => `${normalizarCodigo(codigo).toLowerCase()}@${DOMINIO_EQUIPES}`;

// Chamada à API admin do Supabase Auth com a service role (só no servidor).
export async function authAdmin(caminho, { method = "GET", body } = {}) {
  const chave = process.env.SUPABASE_SERVICE_ROLE_KEY;
  const r = await fetch(`${process.env.SUPABASE_URL}/auth/v1/admin/${caminho}`, {
    method,
    headers: { apikey: chave, Authorization: `Bearer ${chave}`, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const dados = await r.json().catch(() => ({}));
  return { ok: r.ok, status: r.status, dados };
}

// RPC com o JWT do próprio usuário (as funções do banco validam o papel).
export async function rpcUsuario(token, funcao, params = {}) {
  const r = await fetch(`${process.env.SUPABASE_URL}/rest/v1/rpc/${funcao}`, {
    method: "POST",
    headers: {
      apikey: process.env.SUPABASE_ANON_KEY,
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(params),
  });
  const dados = await r.json().catch(() => ({}));
  return { ok: r.ok, status: r.status, dados };
}

export function lerCorpo(req) {
  if (req.body && typeof req.body === "object") return req.body;
  try {
    return JSON.parse(req.body || "{}");
  } catch {
    return {};
  }
}

export function faltandoEnv(nomes) {
  return nomes.filter((n) => !process.env[n]);
}
