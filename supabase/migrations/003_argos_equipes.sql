-- ARGOS — acesso das equipes de campo e avisos com confirmação.
--
-- Cada equipe (código NI2...) tem uma conta própria no Supabase Auth com um
-- e-mail sintético (<código>@<domínio das equipes>). A conta é criada pela
-- função /api/equipe-cadastro do Vercel (service role), que grava o código
-- em app_metadata.equipe — campo que o usuário não consegue alterar. Assim
-- ninguém vira "equipe" por um cadastro comum.
--
-- As contas de equipe:
-- - entram em argos_acessos com papel 'equipe' e precisam de aprovação;
-- - não recebem os pushes da gestão (argos_push_destinos), só os da equipe
--   (argos_push_destinos_equipe);
-- - veem só os próprios avisos em argos_avisos_equipe e precisam confirmar
--   cada um (argos_avisos_confirmar). A gestão vê confirmações e pendências.
--
-- Depende de 001_argos_acessos.sql e 002_argos_push.sql.

-- ------------------------------------------------------------------
-- Contas de equipe em argos_acessos
-- ------------------------------------------------------------------
alter table public.argos_acessos drop constraint if exists argos_acessos_papel_check;
alter table public.argos_acessos
  add constraint argos_acessos_papel_check check (papel in ('admin', 'usuario', 'equipe'));

alter table public.argos_acessos add column if not exists equipe text unique;
alter table public.argos_acessos add column if not exists reset_solicitado_em timestamptz;
alter table public.argos_acessos add column if not exists trocar_senha boolean not null default false;

-- Equipe do usuário logado (aprovado), ou null.
create or replace function public.argos_minha_equipe()
returns text
language sql
stable
security definer
set search_path = public
as $$
  select equipe from public.argos_acessos
   where user_id = auth.uid() and papel = 'equipe' and status = 'aprovado';
$$;

-- Gestão = usuário aprovado que não é equipe (admin ou usuario).
create or replace function public.argos_eh_gestao()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1 from public.argos_acessos
    where user_id = auth.uid() and papel in ('admin', 'usuario') and status = 'aprovado'
  );
$$;

-- Igual à da 001, mas reconhece contas de equipe (app_metadata.equipe).
-- Uma equipe nunca vira o primeiro admin.
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
         nullif(upper(trim(u.raw_app_meta_data ->> 'equipe')), '')
    into v_email, v_nome, v_equipe
    from auth.users u where u.id = v_uid;

  if v_equipe is not null then
    insert into public.argos_acessos (user_id, email, nome, papel, equipe)
    values (v_uid, v_email, coalesce(v_nome, v_equipe), 'equipe', v_equipe)
    on conflict (user_id) do nothing;
    select * into v_linha from public.argos_acessos where user_id = v_uid;
    return v_linha;
  end if;

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

-- Igual à da 001; para equipes o "último acesso" é a última visualização do
-- painel, então registra com resolução de 1 min (gestão continua 5 min).
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
     and (ultimo_acesso_em is null
          or ultimo_acesso_em < now() - case when papel = 'equipe' then interval '1 minute' else interval '5 minutes' end);

  select * into v_linha from public.argos_acessos where user_id = auth.uid();
  return v_linha;
end;
$$;

-- Igual à da 001, mas conta de equipe não vira admin nem usuário da gestão.
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
  if exists (select 1 from public.argos_acessos where user_id = p_alvo and papel = 'equipe') then
    raise exception 'Contas de equipe não podem mudar de papel.' using errcode = '22023';
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
-- Senha das equipes (não há e-mail real para o reset do Supabase)
-- ------------------------------------------------------------------

-- Tela de login da equipe: "Esqueci a senha". Não revela se o código existe.
create or replace function public.argos_equipe_pedir_reset(p_codigo text)
returns void
language sql
volatile
security definer
set search_path = public
as $$
  update public.argos_acessos
     set reset_solicitado_em = now()
   where equipe = upper(trim(p_codigo))
     and (reset_solicitado_em is null or reset_solicitado_em < now() - interval '1 minute');
$$;

-- Chamada pela API /api/admin-equipe com o JWT do admin, ANTES de trocar a
-- senha pela service role: valida que quem chama é admin e que o alvo é uma
-- equipe, e obriga a equipe a trocar a senha temporária no próximo login.
create or replace function public.argos_equipe_preparar_senha(p_alvo uuid)
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
  update public.argos_acessos
     set trocar_senha = true, reset_solicitado_em = null
   where user_id = p_alvo and papel = 'equipe'
  returning * into v_linha;
  if not found then
    raise exception 'Conta de equipe não encontrada.' using errcode = 'P0002';
  end if;
  return v_linha;
end;
$$;

-- Também valida admin/equipe antes da exclusão da conta pela API.
create or replace function public.argos_equipe_validar_exclusao(p_alvo uuid)
returns public.argos_acessos
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  v_linha public.argos_acessos;
begin
  perform public._argos_exigir_admin();
  select * into v_linha from public.argos_acessos where user_id = p_alvo and papel = 'equipe';
  if not found then
    raise exception 'Conta de equipe não encontrada.' using errcode = 'P0002';
  end if;
  return v_linha;
end;
$$;

-- A equipe trocou a senha temporária.
create or replace function public.argos_senha_trocada()
returns void
language sql
volatile
security definer
set search_path = public
as $$
  update public.argos_acessos
     set trocar_senha = false, reset_solicitado_em = null
   where user_id = auth.uid();
$$;

-- ------------------------------------------------------------------
-- Push: gestão e equipes separadas
-- ------------------------------------------------------------------

-- Igual à da 002, mas sem as contas de equipe.
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
     where a.status = 'aprovado' and a.papel <> 'equipe';
end;
$$;

create or replace function public.argos_push_destinos_equipe(p_chave text, p_equipe text)
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
     where a.status = 'aprovado' and a.papel = 'equipe' and a.equipe = upper(trim(p_equipe));
end;
$$;

-- ------------------------------------------------------------------
-- Avisos das equipes
-- ------------------------------------------------------------------
create table if not exists public.argos_avisos_equipe (
  id               bigserial primary key,
  equipe           text not null,
  tipo             text not null check (tipo in ('designada', 'vencer', 'vencida')),
  nivel            text,
  tdc              text not null,
  ordem            text,
  vencimento       timestamptz,
  titulo           text not null,
  mensagem         text,
  criado_em        timestamptz not null default now(),
  status           text not null default 'pendente' check (status in ('pendente', 'confirmado', 'dispensado')),
  confirmado_em    timestamptz,
  confirmado_por   uuid references auth.users (id) on delete set null,
  reenvios         int not null default 0,
  ultimo_envio_em  timestamptz not null default now()
);
create index if not exists argos_avisos_equipe_status on public.argos_avisos_equipe (equipe, status);
create index if not exists argos_avisos_equipe_criado on public.argos_avisos_equipe (criado_em desc);

alter table public.argos_avisos_equipe enable row level security;
revoke all on table public.argos_avisos_equipe from anon;
revoke all on table public.argos_avisos_equipe from authenticated;
grant select on table public.argos_avisos_equipe to authenticated;

-- Leitura: a gestão vê tudo; a equipe só os próprios avisos.
drop policy if exists argos_avisos_equipe_select on public.argos_avisos_equipe;
create policy argos_avisos_equipe_select on public.argos_avisos_equipe
  for select to authenticated
  using (public.argos_eh_gestao() or equipe = public.argos_minha_equipe());

-- Bot: grava os avisos (um por ordem e evento). Só entram os das equipes que
-- têm conta aprovada; devolve as linhas criadas.
-- p_avisos: [{equipe, tipo, nivel, tdc, ordem, vencimento, titulo, mensagem}]
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
      insert into public.argos_avisos_equipe (equipe, tipo, nivel, tdc, ordem, vencimento, titulo, mensagem)
      select upper(trim(x.equipe)), x.tipo, x.nivel, x.tdc, x.ordem, x.vencimento, x.titulo, x.mensagem
        from jsonb_to_recordset(p_avisos)
             as x(equipe text, tipo text, nivel text, tdc text, ordem text, vencimento timestamptz, titulo text, mensagem text)
       where exists (
         select 1 from public.argos_acessos a
          where a.papel = 'equipe' and a.status = 'aprovado' and a.equipe = upper(trim(x.equipe))
       )
      returning *
    )
    select * from criados;
end;
$$;

-- Bot: avisos ainda pendentes (para reenviar ou dispensar).
create or replace function public.argos_avisos_pendentes(p_chave text)
returns setof public.argos_avisos_equipe
language plpgsql
stable
security definer
set search_path = public
as $$
begin
  perform public._argos_push_exigir_chave(p_chave);
  return query select * from public.argos_avisos_equipe where status = 'pendente' order by id;
end;
$$;

-- Bot: registra um reenvio do push.
create or replace function public.argos_avisos_marcar_envio(p_chave text, p_ids bigint[])
returns void
language plpgsql
volatile
security definer
set search_path = public
as $$
begin
  perform public._argos_push_exigir_chave(p_chave);
  update public.argos_avisos_equipe
     set reenvios = reenvios + 1, ultimo_envio_em = now()
   where id = any (p_ids) and status = 'pendente';
end;
$$;

-- Bot: a ordem saiu da equipe (finalizada ou redesignada) — o aviso deixa de
-- exigir confirmação.
create or replace function public.argos_avisos_dispensar(p_chave text, p_ids bigint[])
returns void
language plpgsql
volatile
security definer
set search_path = public
as $$
begin
  perform public._argos_push_exigir_chave(p_chave);
  update public.argos_avisos_equipe
     set status = 'dispensado'
   where id = any (p_ids) and status = 'pendente';
end;
$$;

-- Equipe: "Confirmo que visualizei". Só confirma avisos da própria equipe.
create or replace function public.argos_avisos_confirmar(p_ids bigint[])
returns int
language plpgsql
volatile
security definer
set search_path = public
as $$
declare
  v_equipe text := public.argos_minha_equipe();
  v_n int;
begin
  if v_equipe is null then
    raise exception 'Apenas equipes aprovadas confirmam avisos.' using errcode = '42501';
  end if;
  update public.argos_avisos_equipe
     set status = 'confirmado', confirmado_em = now(), confirmado_por = auth.uid()
   where id = any (p_ids) and equipe = v_equipe and status = 'pendente';
  get diagnostics v_n = row_count;
  return v_n;
end;
$$;

-- Gestão: resumo por equipe para o painel (conta, última visualização,
-- aparelhos com push e avisos pendentes).
create or replace function public.argos_equipes_resumo()
returns table (
  equipe text, user_id uuid, status text, ultimo_acesso_em timestamptz, reset_solicitado_em timestamptz,
  aparelhos int, pendentes int, pendente_desde timestamptz
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
    select a.equipe, a.user_id, a.status, a.ultimo_acesso_em, a.reset_solicitado_em,
           (select count(*)::int from public.argos_push_inscricoes i where i.user_id = a.user_id),
           (select count(*)::int from public.argos_avisos_equipe v where v.equipe = a.equipe and v.status = 'pendente'),
           (select min(v.criado_em) from public.argos_avisos_equipe v where v.equipe = a.equipe and v.status = 'pendente')
      from public.argos_acessos a
     where a.papel = 'equipe';
end;
$$;

-- ------------------------------------------------------------------
-- Permissões
-- ------------------------------------------------------------------
revoke all on function public.argos_minha_equipe() from public, anon;
revoke all on function public.argos_eh_gestao() from public, anon;
revoke all on function public.argos_equipe_pedir_reset(text) from public;
revoke all on function public.argos_equipe_preparar_senha(uuid) from public, anon;
revoke all on function public.argos_equipe_validar_exclusao(uuid) from public, anon;
revoke all on function public.argos_senha_trocada() from public, anon;
revoke all on function public.argos_push_destinos_equipe(text, text) from public;
revoke all on function public.argos_avisos_criar(text, jsonb) from public;
revoke all on function public.argos_avisos_pendentes(text) from public;
revoke all on function public.argos_avisos_marcar_envio(text, bigint[]) from public;
revoke all on function public.argos_avisos_dispensar(text, bigint[]) from public;
revoke all on function public.argos_avisos_confirmar(bigint[]) from public, anon;
revoke all on function public.argos_equipes_resumo() from public, anon;

grant execute on function public.argos_minha_equipe() to authenticated;
grant execute on function public.argos_eh_gestao() to authenticated;
grant execute on function public.argos_equipe_pedir_reset(text) to anon, authenticated;
grant execute on function public.argos_equipe_preparar_senha(uuid) to authenticated;
grant execute on function public.argos_equipe_validar_exclusao(uuid) to authenticated;
grant execute on function public.argos_senha_trocada() to authenticated;
grant execute on function public.argos_avisos_confirmar(bigint[]) to authenticated;
grant execute on function public.argos_equipes_resumo() to authenticated;
-- O bot chama com a anon key; a proteção é a chave do bot.
grant execute on function public.argos_push_destinos_equipe(text, text) to anon;
grant execute on function public.argos_avisos_criar(text, jsonb) to anon;
grant execute on function public.argos_avisos_pendentes(text) to anon;
grant execute on function public.argos_avisos_marcar_envio(text, bigint[]) to anon;
grant execute on function public.argos_avisos_dispensar(text, bigint[]) to anon;
