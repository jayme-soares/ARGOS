// Ações do admin sobre contas de equipe que exigem a service role:
// - "redefinir_senha": gera uma senha temporária (mostrada ao admin para
//   repassar à equipe); a equipe é obrigada a trocá-la no próximo login.
// - "excluir": apaga a conta (libera o código para um novo cadastro).
//
// Quem chama precisa ser admin: a validação é feita pelas funções do banco
// (argos_equipe_preparar_senha / argos_equipe_validar_exclusao) com o JWT do
// próprio admin, ANTES de qualquer chamada com a service role.
import { randomInt } from "node:crypto";
import { authAdmin, faltandoEnv, lerCorpo, rpcUsuario } from "./_equipes.js";

// Sem caracteres ambíguos (0/O, 1/l/I), fácil de ditar por telefone.
const ALFABETO = "abcdefghjkmnpqrstuvwxyz23456789";
const senhaTemporaria = () => Array.from({ length: 10 }, () => ALFABETO[randomInt(ALFABETO.length)]).join("");

export default async function handler(req, res) {
  res.setHeader("Cache-Control", "no-store");
  if (req.method !== "POST") {
    return res.status(405).json({ erro: "Use POST." });
  }
  const faltando = faltandoEnv(["SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY"]);
  if (faltando.length) {
    return res.status(500).json({ erro: `Variáveis não configuradas no Vercel: ${faltando.join(", ")}.` });
  }
  const [tipo, token] = (req.headers.authorization || "").split(" ");
  if (tipo !== "Bearer" || !token) {
    return res.status(401).json({ erro: "Faça login." });
  }

  const { acao, alvo } = lerCorpo(req);
  if (!/^[0-9a-f-]{36}$/i.test(alvo || "")) {
    return res.status(400).json({ erro: "Conta inválida." });
  }

  const validar = acao === "redefinir_senha" ? "argos_equipe_preparar_senha"
    : acao === "excluir" ? "argos_equipe_validar_exclusao" : null;
  if (!validar) {
    return res.status(400).json({ erro: "Ação inválida." });
  }
  const v = await rpcUsuario(token, validar, { p_alvo: alvo });
  if (!v.ok) {
    const status = v.status === 401 ? 401 : 403;
    return res.status(status).json({ erro: v.dados?.message || "Sem permissão." });
  }

  if (acao === "redefinir_senha") {
    const senha = senhaTemporaria();
    const r = await authAdmin(`users/${alvo}`, { method: "PUT", body: { password: senha } });
    if (!r.ok) {
      return res.status(502).json({ erro: `Falha ao trocar a senha (${r.status}). ${r.dados?.msg || ""}`.trim() });
    }
    return res.status(200).json({ equipe: v.dados?.equipe, senha });
  }

  const r = await authAdmin(`users/${alvo}`, { method: "DELETE" });
  if (!r.ok) {
    return res.status(502).json({ erro: `Falha ao excluir a conta (${r.status}). ${r.dados?.msg || ""}`.trim() });
  }
  return res.status(200).json({ equipe: v.dados?.equipe });
}
