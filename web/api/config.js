// Configuração pública do Supabase para o navegador. A anon key é pública
// por definição (a segurança vem do RLS e das funções security definer);
// vem de variável de ambiente só para não ficar fixa no código.
export default function handler(req, res) {
  res.setHeader("Cache-Control", "public, max-age=300");
  if (!process.env.SUPABASE_URL || !process.env.SUPABASE_ANON_KEY) {
    return res.status(500).json({ erro: "SUPABASE_URL/SUPABASE_ANON_KEY não configurados no Vercel." });
  }
  return res.status(200).json({
    supabaseUrl: process.env.SUPABASE_URL,
    supabaseAnonKey: process.env.SUPABASE_ANON_KEY,
    dominioEquipes: process.env.ARGOS_DOMINIO_EQUIPES || "equipes.argos.local",
  });
}
