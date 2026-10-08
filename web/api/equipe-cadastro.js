// Cadastro de uma equipe de campo (código NI2... + senha).
//
// Cria a conta no Supabase Auth pela service role, já confirmada, com o
// código em app_metadata.equipe (o usuário não consegue alterar esse campo,
// então ninguém vira "equipe" por um cadastro comum). A conta nasce pendente
// em argos_acessos no primeiro login e precisa da aprovação de um admin.
import { authAdmin, codigoValido, emailDaEquipe, faltandoEnv, lerCorpo, normalizarCodigo } from "./_equipes.js";

export default async function handler(req, res) {
  res.setHeader("Cache-Control", "no-store");
  if (req.method !== "POST") {
    return res.status(405).json({ erro: "Use POST." });
  }
  const faltando = faltandoEnv(["SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"]);
  if (faltando.length) {
    return res.status(500).json({ erro: `Variáveis não configuradas no Vercel: ${faltando.join(", ")}.` });
  }

  const { codigo, senha } = lerCorpo(req);
  const equipe = normalizarCodigo(codigo);
  if (!codigoValido(equipe)) {
    return res.status(400).json({ erro: "Código de equipe inválido. Use o código que começa com NI2." });
  }
  if (typeof senha !== "string" || senha.length < 8) {
    return res.status(400).json({ erro: "A senha precisa ter pelo menos 8 caracteres." });
  }

  const r = await authAdmin("users", {
    method: "POST",
    body: {
      email: emailDaEquipe(equipe),
      password: senha,
      email_confirm: true,
      app_metadata: { equipe },
      user_metadata: { nome: equipe },
    },
  });
  if (r.ok) {
    return res.status(201).json({ equipe });
  }
  const msg = `${r.dados?.msg || r.dados?.message || r.dados?.error_description || ""}`;
  if (r.status === 422 && /already|exist|registered/i.test(msg + (r.dados?.error_code || ""))) {
    return res.status(409).json({ erro: "Esta equipe já tem acesso cadastrado. Entre com a senha ou use \"Esqueci a senha\"." });
  }
  if (r.status === 422 && /password/i.test(msg)) {
    return res.status(400).json({ erro: `Senha recusada: ${msg}` });
  }
  return res.status(502).json({ erro: `Falha ao criar o acesso (${r.status}). ${msg}`.trim() });
}
