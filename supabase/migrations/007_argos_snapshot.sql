-- ARGOS — cópia de reserva do snapshot do bot no Supabase.
--
-- O painel lê o snapshot do Upstash. Se o Upstash falhar (ex.: estourou a
-- banda do plano gratuito e responde 403), o painel lê esta cópia, que o bot
-- grava junto com a do Upstash. Mesmas chaves e mesmo formato do Upstash
-- (ver argos/publicador.py):
--   <snapshot_key>        gzip+base64 ("gz:..."), tudo menos o campo
--   <snapshot_key>:campo  "<hash>:gz:..." com a seção campo
--
-- Quem grava: o bot, pela função argos_snapshot_gravar, com a anon key e a
-- chave do bot (a mesma ARGOS_PUSH_CHAVE das notificações).
-- Quem lê: só o Vercel (web/api/_snapshot.js), com a service_role. Ninguém
-- lê pelo navegador: o snapshot tem dados de cliente.
--
-- Depende de 002 (chave do bot).

create table if not exists public.argos_snapshot (
  chave         text primary key,
  valor         text not null,
  atualizado_em timestamptz not null default now()
);

alter table public.argos_snapshot enable row level security;
revoke all on table public.argos_snapshot from anon, authenticated;

-- p_itens: {"<chave>": "<valor>", ...}. Grava tudo de uma vez.
create or replace function public.argos_snapshot_gravar(p_chave text, p_itens jsonb)
returns void
language plpgsql
volatile
security definer
set search_path = public
as $$
begin
  perform public._argos_push_exigir_chave(p_chave);
  insert into public.argos_snapshot (chave, valor, atualizado_em)
  select key, value, now() from jsonb_each_text(p_itens)
  on conflict (chave) do update set valor = excluded.valor, atualizado_em = excluded.atualizado_em;
end;
$$;

revoke all on function public.argos_snapshot_gravar(text, jsonb) from public, anon, authenticated;
-- O bot chama com a anon key; a proteção é a chave do bot.
grant execute on function public.argos_snapshot_gravar(text, jsonb) to anon;
