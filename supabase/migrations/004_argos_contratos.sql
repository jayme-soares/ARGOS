-- ARGOS — contratos (SOC e Negociação) e administrador geral.
--
-- - Cada usuário da gestão tem a lista de contratos que pode ver
--   (argos_acessos.contratos). Quem já estava aprovado fica com {soc}; quem
--   for aprovado daqui em diante começa sem contrato e vê "aguardando
--   liberação de contrato" até o administrador geral definir.
-- - Administrador geral (super_admin): um só. Vê todos os contratos, é o
--   único que define os contratos de cada usuário e que promove/rebaixa
--   admins. Ninguém altera o status/papel dele pelas funções.
-- - Os demais admins só aprovam, recusam e revogam acessos.
-- - Contas de equipe pertencem a um contrato (argos_acessos.contrato). O
--   código da equipe é único dentro do contrato. Avisos, pushes e o resumo
--   de equipes passam a ser por contrato.
--
-- Depende de 001, 002 e 003. ANTES DE RODAR, troque o e-mail abaixo pelo
-- e-mail com que o administrador geral entra no painel (a migração aborta se
-- ele não for encontrado em argos_acessos).
--
-- Rode no SQL Editor do Supabase ANTES de atualizar o bot na VPS e o Vercel.

do $$
declare
  v_email text := 'TROQUE-PELO-EMAIL-DO-ADMIN-GERAL';
begin
  if not exists (select 1 from public.argos_acessos where lower(email) = lower(trim(v_email))) then
    raise exception 'Administrador geral não encontrado em argos_acessos: %. Edite v_email no topo da migração.', v_email;
  end if;
  perform set_config('argos.email_super_admin', lower(trim(v_email)), false);
end;
$$;

-- ------------------------------------------------------------------
-- Colunas
-- ------------------------------------------------------------------
alter table public.argos_acessos add column if not exists contratos text[] not null default '{}';
alter table public.argos_acessos add column if not exists super_admin boolean not null default false;
alter table public.argos_acessos add column if not exists contrato text;

alter table public.argos_acessos drop constraint if exists argos_acessos_contratos_check;
alter table public.argos_acessos
  add constraint argos_acessos_contratos_check check (contratos <@ array['soc', 'negociacao']::text[]);

-- Backfill: gestão já aprovada vê a SOC (era o único contrato); equipes
-- existentes são todas NI2 da SOC.
update public.argos_acessos set contratos = '{soc}' where papel <> 'equipe' and status = 'aprovado' and contratos = '{}';
update public.argos_acessos set contrato = 'soc' where papel = 'equipe' and contrato is null;
alter table public.argos_acessos drop constraint if exists argos_acessos_contrato_check;
alter table public.argos_acessos
  add constraint argos_acessos_contrato_check check (
    (papel = 'equipe') = (contrato is not null) and (contrato is null or contrato in ('soc', 'negociacao'))
  );

-- Código da equipe: único por contrato (era único no sistema todo).
alter table public.argos_acessos drop constraint if exists argos_acessos_equipe_key;
drop index if exists public.argos_acessos_contrato_equipe;
create unique index argos_acessos_contrato_equipe on public.argos_acessos (contrato, equipe) where equipe is not null;

-- Um único administrador geral.
drop index if exists public.argos_acessos_um_super_admin;
create unique index argos_acessos_um_super_admin on public.argos_acessos (super_admin) where super_admin;
update public.argos_acessos
   set super_admin = true, papel = 'admin', status = 'aprovado', contratos = '{soc,negociacao}'
 where lower(email) = current_setting('argos.email_super_admin');

-- ------------------------------------------------------------------
-- Helpers
-- ------------------------------------------------------------------
create or replace function public.argos_eh_super_admin()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1 from public.argos_acessos
    where user_id = auth.uid() and super_admin and status = 'aprovado'
  );
$$;

-- Contratos que a linha pode ver: todos para o administrador geral.
create or replace function public.argos_contratos_efetivos(p_linha public.argos_acessos)
returns text[]
language sql
immutable
as $$
  select case when p_linha.super_admin then array['soc', 'negociacao']::text[] else p_linha.contratos end;
$$;

-- Gestão aprovada com acesso ao contrato.
create or replace function public.argos_tem_contrato(p_contrato text)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1 from public.argos_acessos a
    where a.user_id = auth.uid() and a.papel in ('admin', 'usuario') and a.status = 'aprovado'
      and p_contrato = any (public.argos_contratos_efetivos(a))
  );
$$;

-- Contrato da equipe logada (aprovada), ou null.
create or replace function public.argos_meu_contrato_equipe()
returns text
language sql
stable
security definer
set search_path = public
as $$
  select contrato from public.argos_acessos
   where user_id = auth.uid() and papel = 'equipe' and status = 'aprovado';
$$;

create or replace function public._argos_proteger_super_admin(p_alvo uuid)
returns void
language plpgsql
stable
security definer
set search_path = public
as $$
begin
  if exists (select 1 from public.argos_acessos where user_id = p_alvo and super_admin) then
    raise exception 'O administrador geral não pode ser alterado.' using errcode = '42501';
  end if;
end;
$$;

-- ------------------------------------------------------------------
-- Usuário
-- ------------------------------------------------------------------

-- Igual à da 003, mas a conta de equipe leva o contrato (app_metadata.contrato,
-- padrão soc). Gestão nova começa sem contrato.
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
  v_equipe text;
  v_contrato text;
begin
  if v_uid is null then
    raise exception 'Não autenticado.' using errcode = '28000';
  end if;

  select * into v_linha from public.argos_acessos where user_id = v_uid;
  if found then
    return v_linha;
  end if;

  select u.email,
         coalesce(u.raw_user_meta_data ->> 'nome', u.raw_user_meta_data ->> 'full_name', u.raw_user_meta_data ->> 'name'),
         nullif(upper(trim(u.raw_app_meta_data ->> 'equipe')), ''),
         coalesce(nullif(trim(u.raw_app_meta_data ->> 'contrato'), ''), 'soc')
    into v_email, v_nome, v_equipe, v_contrato
    from auth.users u where u.id = v_uid;

  if v_equipe is not null then
    insert into public.argos_acessos (user_id, email, nome, papel, equipe, contrato)
    values (v_uid, v_email, coalesce(v_nome, v_equipe), 'equipe', v_equipe, v_contrato)
    on conflict (user_id) do nothing;
    select * into v_linha from public.argos_acessos where user_id = v_uid;
    return v_linha;
  end if;

  perform pg_advisory_xact_lock(hashtext('argos_primeiro_admin'));

  if not exists (select 1 from public.argos_acessos where papel = 'admin') then
    insert into public.argos_acessos (user_id, email, nome, status, papel, decidido_em, ultimo_acesso_em, super_admin)
    values (v_uid, v_email, v_nome, 'aprovado', 'admin', now(), now(), true)
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

-- ------------------------------------------------------------------
-- Admin
-- ------------------------------------------------------------------

-- Igual à da 001, mas não mexe no administrador geral.
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
  perform public._argos_proteger_super_admin(p_alvo);
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

-- Igual à da 003, mas só o administrador geral promove/rebaixa, e ele
-- mesmo não muda de papel.
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
  if not public.argos_eh_super_admin() then
    raise exception 'Apenas o administrador geral define os administradores.' using errcode = '42501';
  end if;
  if p_papel not in ('admin', 'usuario') then
    raise exception 'Papel inválido: %', p_papel using errcode = '22023';
  end if;
  if exists (select 1 from public.argos_acessos where user_id = p_alvo and papel = 'equipe') then
    raise exception 'Contas de equipe não podem mudar de papel.' using errcode = '22023';
  end if;
  perform public._argos_proteger_super_admin(p_alvo);
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

-- Só o administrador geral: quais painéis (contratos) a pessoa da gestão vê.
create or replace function public.argos_definir_contratos(p_alvo uuid, p_contratos text[])
returns public.argos_acessos
language plpgsql
volatile
security definer
set search_path = public
as $$
declare
  v_linha public.argos_acessos;
  v_contratos text[];
begin
  if not public.argos_eh_super_admin() then
    raise exception 'Apenas o administrador geral define os contratos.' using errcode = '42501';
  end if;
  select coalesce(array_agg(distinct c order by c), '{}') into v_contratos from unnest(p_contratos) c;
  if not v_contratos <@ array['soc', 'negociacao']::text[] then
    raise exception 'Contrato inválido: %', p_contratos using errcode = '22023';
  end if;
  perform public._argos_proteger_super_admin(p_alvo);

  update public.argos_acessos
     set contratos = v_contratos, decidido_em = now(), decidido_por = auth.uid()
   where user_id = p_alvo and papel <> 'equipe'
  returning * into v_linha;

  if not found then
    raise exception 'Usuário da gestão não encontrado.' using errcode = 'P0002';
  end if;
  return v_linha;
end;
$$;

-- ------------------------------------------------------------------
-- Equipes
-- ------------------------------------------------------------------

-- "Esqueci a senha": agora com o contrato (o código só é único dentro dele).
drop function if exists public.argos_equipe_pedir_reset(text);
create or replace function public.argos_equipe_pedir_reset(p_codigo text, p_contrato text default 'soc')
returns void
language sql
volatile
security definer
set search_path = public
as $$
  update public.argos_acessos
     set reset_solicitado_em = now()
   where equipe = upper(trim(p_codigo))
     and contrato = p_contrato
     and (reset_solicitado_em is null or reset_solicitado_em < now() - interval '1 minute');
$$;

-- ------------------------------------------------------------------
-- Push por contrato
-- ------------------------------------------------------------------

-- Gestão com acesso ao contrato (administrador geral incluso).
create or replace function public.argos_push_destinos(p_chave text, p_contrato text)
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
     where a.status = 'aprovado' and a.papel <> 'equipe'
       and p_contrato = any (public.argos_contratos_efetivos(a));
end;
$$;

-- Versão antiga (bot ainda não atualizado): só a SOC.
create or replace function public.argos_push_destinos(p_chave text)
returns table (endpoint text, p256dh text, auth text)
language sql
stable
security definer
set search_path = public
as $$
  select * from public.argos_push_destinos(p_chave, 'soc');
$$;

create or replace function public.argos_push_destinos_equipe(p_chave text, p_contrato text, p_equipe text)
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
     where a.status = 'aprovado' and a.papel = 'equipe'
       and a.contrato = p_contrato and a.equipe = upper(trim(p_equipe));
end;
$$;

create or replace function public.argos_push_destinos_equipe(p_chave text, p_equipe text)
returns table (endpoint text, p256dh text, auth text)
language sql
stable
security definer
set search_path = public
as $$
  select * from public.argos_push_destinos_equipe(p_chave, 'soc', p_equipe);
$$;

-- ------------------------------------------------------------------
-- Avisos por contrato
-- ------------------------------------------------------------------
alter table public.argos_avisos_equipe add column if not exists contrato text not null default 'soc';
alter table public.argos_avisos_equipe drop constraint if exists argos_avisos_equipe_contrato_check;
alter table public.argos_avisos_equipe
  add constraint argos_avisos_equipe_contrato_check check (contrato in ('soc', 'negociacao'));
drop index if exists public.argos_avisos_equipe_status;
create index if not exists argos_avisos_equipe_contrato_status on public.argos_avisos_equipe (contrato, equipe, status);

-- Leitura: a gestão vê os avisos dos contratos dela; a equipe, só os próprios.
drop policy if exists argos_avisos_equipe_select on public.argos_avisos_equipe;
create policy argos_avisos_equipe_select on public.argos_avisos_equipe
  for select to authenticated
  using (
    public.argos_tem_contrato(contrato)
    or (equipe = public.argos_minha_equipe() and contrato = public.argos_meu_contrato_equipe())
  );

-- p_avisos: [{contrato, equipe, tipo, nivel, tdc, ordem, vencimento, titulo, mensagem}]
-- (sem contrato = soc, como o bot antigo mandava).
create or replace function public.argos_avisos_criar(p_chave text, p_avisos jsonb)
returns setof public.argos_avisos_equipe
language plpgsql
volatile
security definer
set search_path = public
as $$
begin
  perform public._argos_push_exigir_chave(p_chave);
  return query
    with criados as (
      insert into public.argos_avisos_equipe (contrato, equipe, tipo, nivel, tdc, ordem, vencimento, titulo, mensagem)
      select coalesce(x.contrato, 'soc'), upper(trim(x.equipe)), x.tipo, x.nivel, x.tdc, x.ordem, x.vencimento, x.titulo, x.mensagem
        from jsonb_to_recordset(p_avisos)
             as x(contrato text, equipe text, tipo text, nivel text, tdc text, ordem text, vencimento timestamptz,
                  titulo text, mensagem text)
       where exists (
         select 1 from public.argos_acessos a
          where a.papel = 'equipe' and a.status = 'aprovado'
            and a.contrato = coalesce(x.contrato, 'soc') and a.equipe = upper(trim(x.equipe))
       )
      returning *
    )
    select * from criados;
end;
$$;

create or replace function public.argos_avisos_pendentes(p_chave text, p_contrato text)
returns setof public.argos_avisos_equipe
language plpgsql
stable
security definer
set search_path = public
as $$
begin
  perform public._argos_push_exigir_chave(p_chave);
  return query
    select * from public.argos_avisos_equipe where status = 'pendente' and contrato = p_contrato order by id;
end;
$$;

-- Versão antiga: só a SOC (senão o bot antigo dispensaria os avisos da
-- Negociação por não achar as ordens nas religas da SOC).
create or replace function public.argos_avisos_pendentes(p_chave text)
returns setof public.argos_avisos_equipe
language sql
stable
security definer
set search_path = public
as $$
  select * from public.argos_avisos_pendentes(p_chave, 'soc');
$$;

create or replace function public.argos_avisos_confirmar(p_ids bigint[])
returns int
language plpgsql
volatile
security definer
set search_path = public
as $$
declare
  v_equipe text := public.argos_minha_equipe();
  v_contrato text := public.argos_meu_contrato_equipe();
  v_n int;
begin
  if v_equipe is null then
    raise exception 'Apenas equipes aprovadas confirmam avisos.' using errcode = '42501';
  end if;
  update public.argos_avisos_equipe
     set status = 'confirmado', confirmado_em = now(), confirmado_por = auth.uid()
   where id = any (p_ids) and equipe = v_equipe and contrato = v_contrato and status = 'pendente';
  get diagnostics v_n = row_count;
  return v_n;
end;
$$;

-- Resumo por equipe, agora com o contrato e só dos contratos de quem pede.
drop function if exists public.argos_equipes_resumo();
create function public.argos_equipes_resumo()
returns table (
  contrato text, equipe text, nome text, user_id uuid, status text, ultimo_acesso_em timestamptz,
  reset_solicitado_em timestamptz, aparelhos int, pendentes int, pendente_desde timestamptz
)
language plpgsql
stable
security definer
set search_path = public
as $$
begin
  if not public.argos_eh_gestao() then
    raise exception 'Apenas a gestão do ARGOS pode fazer isso.' using errcode = '42501';
  end if;
  return query
    select a.contrato, a.equipe, a.nome, a.user_id, a.status, a.ultimo_acesso_em, a.reset_solicitado_em,
           (select count(*)::int from public.argos_push_inscricoes i where i.user_id = a.user_id),
           (select count(*)::int from public.argos_avisos_equipe v
             where v.contrato = a.contrato and v.equipe = a.equipe and v.status = 'pendente'),
           (select min(v.criado_em) from public.argos_avisos_equipe v
             where v.contrato = a.contrato and v.equipe = a.equipe and v.status = 'pendente')
      from public.argos_acessos a
     where a.papel = 'equipe' and public.argos_tem_contrato(a.contrato);
end;
$$;

-- ------------------------------------------------------------------
-- Permissões
-- ------------------------------------------------------------------
revoke all on function public.argos_eh_super_admin() from public, anon;
revoke all on function public.argos_contratos_efetivos(public.argos_acessos) from public, anon;
revoke all on function public.argos_tem_contrato(text) from public, anon;
revoke all on function public.argos_meu_contrato_equipe() from public, anon;
revoke all on function public._argos_proteger_super_admin(uuid) from public, anon, authenticated;
revoke all on function public.argos_definir_contratos(uuid, text[]) from public, anon;
revoke all on function public.argos_equipe_pedir_reset(text, text) from public;
revoke all on function public.argos_push_destinos(text, text) from public;
revoke all on function public.argos_push_destinos(text) from public;
revoke all on function public.argos_push_destinos_equipe(text, text, text) from public;
revoke all on function public.argos_push_destinos_equipe(text, text) from public;
revoke all on function public.argos_avisos_pendentes(text, text) from public;
revoke all on function public.argos_avisos_pendentes(text) from public;
revoke all on function public.argos_equipes_resumo() from public, anon;

grant execute on function public.argos_eh_super_admin() to authenticated;
grant execute on function public.argos_contratos_efetivos(public.argos_acessos) to authenticated;
grant execute on function public.argos_tem_contrato(text) to authenticated;
grant execute on function public.argos_meu_contrato_equipe() to authenticated;
grant execute on function public.argos_definir_contratos(uuid, text[]) to authenticated;
grant execute on function public.argos_equipe_pedir_reset(text, text) to anon, authenticated;
grant execute on function public.argos_equipes_resumo() to authenticated;
-- O bot chama com a anon key; a proteção é a chave do bot.
grant execute on function public.argos_push_destinos(text, text) to anon;
grant execute on function public.argos_push_destinos(text) to anon;
grant execute on function public.argos_push_destinos_equipe(text, text, text) to anon;
grant execute on function public.argos_push_destinos_equipe(text, text) to anon;
grant execute on function public.argos_avisos_pendentes(text, text) to anon;
grant execute on function public.argos_avisos_pendentes(text) to anon;
