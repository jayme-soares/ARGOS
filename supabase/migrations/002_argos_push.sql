-- ARGOS — notificações push pelo próprio painel (Web Push).
--
-- Cada navegador que ativa as notificações vira uma linha em
-- public.argos_push_inscricoes, ligada ao usuário do ARGOS. Só recebe push
-- quem está APROVADO em argos_acessos: revogar o acesso corta os avisos.
--
-- Quem grava: o próprio usuário, pelas funções argos_push_inscrever /
-- argos_push_cancelar (com o JWT dele, direto do navegador).
-- Quem lê: o bot na VPS, pelas funções argos_push_destinos / argos_push_remover,
-- que exigem a chave do bot (ARGOS_PUSH_CHAVE no .env da VPS). Assim a VPS
-- não precisa da service_role do Supabase da empresa.
--
-- Depende de 001_argos_acessos.sql. Não altera nada que já existe.
--
-- Depois de rodar, cadastre a chave do bot (troque o texto pelo mesmo valor
-- de ARGOS_PUSH_CHAVE):
--   insert into public.argos_push_config (id, chave_hash)
--   values (1, encode(sha256(convert_to('COLE-AQUI-A-ARGOS_PUSH_CHAVE', 'UTF8')), 'hex'))
--   on conflict (id) do update set chave_hash = excluded.chave_hash;

create table if not exists public.argos_push_inscricoes (
  endpoint       text primary key,
  user_id        uuid not null references auth.users (id) on delete cascade,
  p256dh         text not null,
  auth           text not null,
  user_agent     text,
  criado_em      timestamptz not null default now(),
  atualizado_em  timestamptz not null default now()
);
create index if not exists argos_push_inscricoes_user_id on public.argos_push_inscricoes (user_id);

-- Guarda só o hash da chave do bot.
create table if not exists public.argos_push_config (
  id          int primary key check (id = 1),
  chave_hash  text not null
);

-- Sem policies: ninguém lê nem escreve direto pela API, só pelas funções.
alter table public.argos_push_inscricoes enable row level security;
alter table public.argos_push_config enable row level security;
revoke all on table public.argos_push_inscricoes from anon, authenticated;
revoke all on table public.argos_push_config from anon, authenticated;

-- ------------------------------------------------------------------
-- Usuário (navegador)
-- ------------------------------------------------------------------

-- Cria ou atualiza a inscrição deste navegador. Se o mesmo navegador já
-- estava inscrito com outro usuário (troca de login), passa a ser do atual.
create or replace function public.argos_push_inscrever(p_endpoint text, p_p256dh text, p_auth text, p_user_agent text default null)
returns void
language plpgsql
volatile
security definer
set search_path = public
as $$
begin
  if auth.uid() is null then
    raise exception 'Não autenticado.' using errcode = '28000';
  end if;
  if not exists (select 1 from public.argos_acessos where user_id = auth.uid() and status = 'aprovado') then
    raise exception 'Acesso ao ARGOS não aprovado.' using errcode = '42501';
  end if;
  if p_endpoint !~ '^https://' or coalesce(p_p256dh, '') = '' or coalesce(p_auth, '') = '' then
    raise exception 'Inscrição inválida.' using errcode = '22023';
  end if;

  insert into public.argos_push_inscricoes (endpoint, user_id, p256dh, auth, user_agent)
  values (p_endpoint, auth.uid(), p_p256dh, p_auth, left(p_user_agent, 300))
  on conflict (endpoint) do update
     set user_id = excluded.user_id,
         p256dh = excluded.p256dh,
         auth = excluded.auth,
         user_agent = excluded.user_agent,
         atualizado_em = now();
end;
$$;

create or replace function public.argos_push_cancelar(p_endpoint text)
returns void
language sql
volatile
security definer
set search_path = public
as $$
  delete from public.argos_push_inscricoes where endpoint = p_endpoint and user_id = auth.uid();
$$;

-- ------------------------------------------------------------------
-- Bot (VPS)
-- ------------------------------------------------------------------

create or replace function public._argos_push_exigir_chave(p_chave text)
returns void
language plpgsql
stable
security definer
set search_path = public
as $$
begin
  if not exists (
    select 1 from public.argos_push_config
    where id = 1 and chave_hash = encode(sha256(convert_to(coalesce(p_chave, ''), 'UTF8')), 'hex')
  ) then
    raise exception 'Chave do bot inválida.' using errcode = '42501';
  end if;
end;
$$;

-- Inscrições de quem está aprovado.
create or replace function public.argos_push_destinos(p_chave text)
returns table (endpoint text, p256dh text, auth text)
language plpgsql
stable
security definer
set search_path = public
as $$
begin
  perform public._argos_push_exigir_chave(p_chave);
  return query
    select i.endpoint, i.p256dh, i.auth
      from public.argos_push_inscricoes i
      join public.argos_acessos a on a.user_id = i.user_id
     where a.status = 'aprovado';
end;
$$;

-- Remove inscrições que o serviço de push informou não existirem mais.
create or replace function public.argos_push_remover(p_chave text, p_endpoints text[])
returns void
language plpgsql
volatile
security definer
set search_path = public
as $$
begin
  perform public._argos_push_exigir_chave(p_chave);
  delete from public.argos_push_inscricoes where endpoint = any (p_endpoints);
end;
$$;

-- ------------------------------------------------------------------
-- Permissões
-- ------------------------------------------------------------------
revoke all on function public.argos_push_inscrever(text, text, text, text) from public, anon;
revoke all on function public.argos_push_cancelar(text) from public, anon;
revoke all on function public._argos_push_exigir_chave(text) from public, anon, authenticated;
revoke all on function public.argos_push_destinos(text) from public;
revoke all on function public.argos_push_remover(text, text[]) from public;

grant execute on function public.argos_push_inscrever(text, text, text, text) to authenticated;
grant execute on function public.argos_push_cancelar(text) to authenticated;
-- O bot chama com a anon key; a proteção é a chave do bot.
grant execute on function public.argos_push_destinos(text) to anon;
grant execute on function public.argos_push_remover(text, text[]) to anon;
