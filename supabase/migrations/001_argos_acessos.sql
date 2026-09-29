-- ARGOS — controle de acesso ao painel.
--
-- Usa os usuários que já existem no Supabase Auth (auth.users) e NÃO altera
-- nenhuma tabela existente: cria só public.argos_acessos e funções argos_*.
--
-- Regras:
-- - Quem loga no painel pela primeira vez vira uma solicitação "pendente".
-- - O PRIMEIRO usuário de todos vira admin aprovado automaticamente.
-- - Admins aprovam/recusam/revogam acessos e promovem/rebaixam admins.
-- - Nunca é possível ficar sem nenhum admin aprovado.
--
-- Escrita só pelas funções (security definer); não há policy de
-- insert/update/delete, então ninguém altera a tabela direto pela API.
--
-- Rode no SQL Editor do Supabase (ou `supabase db push`).

create table if not exists public.argos_acessos (
  user_id          uuid primary key references auth.users (id) on delete cascade,
  email            text not null,
  nome             text,
  status           text not null default 'pendente' check (status in ('pendente', 'aprovado', 'recusado')),
  papel            text not null default 'usuario' check (papel in ('admin', 'usuario')),
  solicitado_em    timestamptz not null default now(),
  decidido_em      timestamptz,
  decidido_por     uuid references auth.users (id) on delete set null,
  ultimo_acesso_em timestamptz
);

alter table public.argos_acessos enable row level security;

-- ------------------------------------------------------------------
-- Helpers
-- ------------------------------------------------------------------
create or replace function public.argos_eh_admin()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1 from public.argos_acessos
    where user_id = auth.uid() and papel = 'admin' and status = 'aprovado'
  );
$$;

create or replace function public._argos_exigir_admin()
returns void
language plpgsql
stable
security definer
set search_path = public
as $$
begin
  if not public.argos_eh_admin() then
    raise exception 'Apenas administradores do ARGOS podem fazer isso.' using errcode = '42501';
  end if;
end;
$$;

-- Leitura: cada um vê a própria linha; admins veem todas.
drop policy if exists argos_acessos_select on public.argos_acessos;
create policy argos_acessos_select on public.argos_acessos
  for select to authenticated
  using (user_id = auth.uid() or public.argos_eh_admin());

-- ------------------------------------------------------------------
-- Usuário
-- ------------------------------------------------------------------

-- Chamada logo após o login. Cria a solicitação (se ainda não existe) e
-- devolve o acesso do usuário. O primeiro usuário vira admin aprovado.
create or replace function public.argos_solicitar_acesso()
returns public.argos_acessos
language plpgsql
volatile
security definer
set search_path = public
as $$
declare
  v_uid uuid := auth.uid();
  v_linha public.argos_acessos;
  v_email text;
  v_nome text;
begin
  if v_uid is null then
    raise exception 'Não autenticado.' using errcode = '28000';
  end if;

  select * into v_linha from public.argos_acessos where user_id = v_uid;
  if found then
    return v_linha;
  end if;

  select u.email,
         coalesce(u.raw_user_meta_data ->> 'nome', u.raw_user_meta_data ->> 'full_name', u.raw_user_meta_data ->> 'name')
    into v_email, v_nome
    from auth.users u where u.id = v_uid;

  -- Serializa a checagem do "primeiro usuário": sem o lock, dois primeiros
  -- logins simultâneos poderiam virar admin os dois.
  perform pg_advisory_xact_lock(hashtext('argos_primeiro_admin'));

  if not exists (select 1 from public.argos_acessos where papel = 'admin') then
    insert into public.argos_acessos (user_id, email, nome, status, papel, decidido_em, ultimo_acesso_em)
    values (v_uid, v_email, v_nome, 'aprovado', 'admin', now(), now())
    returning * into v_linha;
  else
    insert into public.argos_acessos (user_id, email, nome)
    values (v_uid, v_email, v_nome)
    on conflict (user_id) do nothing;
    select * into v_linha from public.argos_acessos where user_id = v_uid;
  end if;

  return v_linha;
end;
$$;

-- Usada pela API do painel a cada leitura de dados: valida o token (o
-- PostgREST rejeita JWT inválido antes de chegar aqui) e devolve o acesso.
-- Atualiza ultimo_acesso_em no máximo a cada 5 min para não escrever a
-- cada atualização automática do painel.
create or replace function public.argos_meu_acesso()
returns public.argos_acessos
language plpgsql
volatile
security definer
set search_path = public
as $$
declare
  v_linha public.argos_acessos;
begin
  update public.argos_acessos
     set ultimo_acesso_em = now()
   where user_id = auth.uid()
     and status = 'aprovado'
     and (ultimo_acesso_em is null or ultimo_acesso_em < now() - interval '5 minutes');

  select * into v_linha from public.argos_acessos where user_id = auth.uid();
  return v_linha;
end;
$$;

-- ------------------------------------------------------------------
-- Admin
-- ------------------------------------------------------------------

create or replace function public._argos_garantir_outro_admin(p_alvo uuid)
returns void
language plpgsql
stable
security definer
set search_path = public
as $$
begin
  if exists (select 1 from public.argos_acessos where user_id = p_alvo and papel = 'admin' and status = 'aprovado')
     and not exists (
       select 1 from public.argos_acessos
       where user_id <> p_alvo and papel = 'admin' and status = 'aprovado'
     ) then
    raise exception 'Este é o último administrador. Promova outra pessoa antes.' using errcode = 'P0001';
  end if;
end;
$$;

-- Aprovar, recusar (pendente) ou revogar (aprovado -> recusado).
create or replace function public.argos_definir_status(p_alvo uuid, p_status text)
returns public.argos_acessos
language plpgsql
volatile
security definer
set search_path = public
as $$
declare
  v_linha public.argos_acessos;
begin
  perform public._argos_exigir_admin();
  if p_status not in ('aprovado', 'recusado') then
    raise exception 'Status inválido: %', p_status using errcode = '22023';
  end if;
  perform pg_advisory_xact_lock(hashtext('argos_primeiro_admin'));
  if p_status <> 'aprovado' then
    perform public._argos_garantir_outro_admin(p_alvo);
  end if;

  update public.argos_acessos
     set status = p_status, decidido_em = now(), decidido_por = auth.uid()
   where user_id = p_alvo
  returning * into v_linha;

  if not found then
    raise exception 'Usuário não encontrado.' using errcode = 'P0002';
  end if;
  return v_linha;
end;
$$;

-- Promover a admin (também aprova) ou rebaixar para usuário.
create or replace function public.argos_definir_papel(p_alvo uuid, p_papel text)
returns public.argos_acessos
language plpgsql
volatile
security definer
set search_path = public
as $$
declare
  v_linha public.argos_acessos;
begin
  perform public._argos_exigir_admin();
  if p_papel not in ('admin', 'usuario') then
    raise exception 'Papel inválido: %', p_papel using errcode = '22023';
  end if;
  perform pg_advisory_xact_lock(hashtext('argos_primeiro_admin'));
  if p_papel = 'usuario' then
    perform public._argos_garantir_outro_admin(p_alvo);
  end if;

  update public.argos_acessos
     set papel = p_papel,
         status = case when p_papel = 'admin' then 'aprovado' else status end,
         decidido_em = now(),
         decidido_por = auth.uid()
   where user_id = p_alvo
  returning * into v_linha;

  if not found then
    raise exception 'Usuário não encontrado.' using errcode = 'P0002';
  end if;
  return v_linha;
end;
$$;

-- ------------------------------------------------------------------
-- Permissões: só usuários logados chamam as funções; helpers internos
-- (prefixo _) não ficam expostos.
-- ------------------------------------------------------------------
revoke all on function public.argos_eh_admin() from public, anon;
revoke all on function public._argos_exigir_admin() from public, anon, authenticated;
revoke all on function public._argos_garantir_outro_admin(uuid) from public, anon, authenticated;
revoke all on function public.argos_solicitar_acesso() from public, anon;
revoke all on function public.argos_meu_acesso() from public, anon;
revoke all on function public.argos_definir_status(uuid, text) from public, anon;
revoke all on function public.argos_definir_papel(uuid, text) from public, anon;

grant execute on function public.argos_eh_admin() to authenticated;
grant execute on function public.argos_solicitar_acesso() to authenticated;
grant execute on function public.argos_meu_acesso() to authenticated;
grant execute on function public.argos_definir_status(uuid, text) to authenticated;
grant execute on function public.argos_definir_papel(uuid, text) to authenticated;

revoke all on table public.argos_acessos from anon;
grant select on table public.argos_acessos to authenticated;
