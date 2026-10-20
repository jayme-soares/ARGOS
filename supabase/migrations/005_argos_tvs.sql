-- ARGOS — TVs do modo kiosk (/tv), pareadas por PIN.
--
-- Igual a "entrar com código" de streaming: a TV abre /tv e mostra um PIN de
-- 6 dígitos; um admin digita o PIN no painel (aba TVs), escolhe o contrato e
-- a TV passa a mostrar o painel daquele contrato. Remover a TV no painel
-- desconecta (ela volta a mostrar um PIN novo).
--
-- O segredo da TV é um token aleatório gerado no Vercel (web/api/tv.js) e
-- guardado no navegador da TV; aqui fica só o hash SHA-256 dele. O PIN só
-- serve para o admin achar a TV pendente: sozinho não dá acesso a nada.
--
-- Depende de 001 e 004.

create table if not exists public.argos_tvs (
  id               uuid primary key default gen_random_uuid(),
  token_hash       text not null unique,
  pin              text,
  status           text not null default 'pendente' check (status in ('pendente', 'aprovado')),
  nome             text,
  contrato         text check (contrato is null or contrato in ('soc', 'negociacao')),
  config           jsonb not null default '{}'::jsonb,
  user_agent       text,
  criado_em        timestamptz not null default now(),
  expira_em        timestamptz,
  aprovado_em      timestamptz,
  aprovado_por     uuid references auth.users (id) on delete set null,
  ultimo_acesso_em timestamptz,
  check (status = 'pendente' or contrato is not null)
);
-- Um PIN não se repete entre as TVs esperando pareamento.
create unique index if not exists argos_tvs_pin_pendente on public.argos_tvs (pin) where status = 'pendente';

alter table public.argos_tvs enable row level security;
revoke all on table public.argos_tvs from anon, authenticated;

-- Só as opções conhecidas: tema (escuro/claro), zoom (0,6 a 2) e COs (Negociação).
create or replace function public._argos_tv_config(p_config jsonb)
returns jsonb
language sql
immutable
as $$
  select jsonb_strip_nulls(jsonb_build_object(
    'tema', case when p_config ->> 'tema' = 'claro' then 'claro' else 'escuro' end,
    'zoom', case when (p_config ->> 'zoom') ~ '^[0-9]+(\.[0-9]+)?$'
                 then least(2, greatest(0.6, (p_config ->> 'zoom')::numeric)) else 1 end,
    'cos', case when jsonb_typeof(p_config -> 'cos') = 'array'
                then (select coalesce(jsonb_agg(trim(c)), '[]'::jsonb)
                        from jsonb_array_elements_text(p_config -> 'cos') c where trim(c) <> '')
                else '[]'::jsonb end
  ));
$$;

-- ------------------------------------------------------------------
-- TV (chamadas pelo Vercel com a anon key; a proteção é o token)
-- ------------------------------------------------------------------

-- Nova TV esperando pareamento: devolve o PIN (vale 10 minutos).
create or replace function public.argos_tv_iniciar(p_token_hash text, p_user_agent text default null)
returns jsonb
language plpgsql
volatile
security definer
set search_path = public
as $$
declare
  v_pin text;
  v_expira timestamptz := now() + interval '10 minutes';
begin
  if p_token_hash !~ '^[0-9a-f]{64}$' then
    raise exception 'Token inválido.' using errcode = '22023';
  end if;
  delete from public.argos_tvs where status = 'pendente' and expira_em < now();
  -- Sem isso, qualquer um com a anon key encheria a tabela de PINs.
  if (select count(*) from public.argos_tvs where status = 'pendente') >= 20 then
    raise exception 'Muitas TVs esperando pareamento. Tente de novo em alguns minutos.' using errcode = '53400';
  end if;
  loop
    v_pin := lpad((floor(random() * 1000000))::int::text, 6, '0');
    exit when not exists (select 1 from public.argos_tvs where status = 'pendente' and pin = v_pin);
  end loop;
  insert into public.argos_tvs (token_hash, pin, expira_em, user_agent)
  values (p_token_hash, v_pin, v_expira, left(p_user_agent, 300));
  return jsonb_build_object('pin', v_pin, 'expira_em', v_expira);
end;
$$;

-- Situação da TV: null se o token não existe (removida) ou o PIN expirou.
create or replace function public.argos_tv_estado(p_token_hash text)
returns jsonb
language plpgsql
volatile
security definer
set search_path = public
as $$
declare
  v public.argos_tvs;
begin
  select * into v from public.argos_tvs where token_hash = p_token_hash;
  if not found or (v.status = 'pendente' and v.expira_em < now()) then
    return null;
  end if;
  if v.status = 'pendente' then
    return jsonb_build_object('status', 'pendente', 'pin', v.pin, 'expira_em', v.expira_em);
  end if;
  if v.ultimo_acesso_em is null or v.ultimo_acesso_em < now() - interval '1 minute' then
    update public.argos_tvs set ultimo_acesso_em = now() where id = v.id;
  end if;
  return jsonb_build_object('status', 'aprovado', 'id', v.id, 'nome', v.nome, 'contrato', v.contrato, 'config', v.config);
end;
$$;

-- ------------------------------------------------------------------
-- Admin (painel, aba TVs)
-- ------------------------------------------------------------------
create or replace function public._argos_tv_exigir_contrato(p_contrato text)
returns void
language plpgsql
stable
security definer
set search_path = public
as $$
begin
  perform public._argos_exigir_admin();
  if p_contrato is null or not public.argos_tem_contrato(p_contrato) then
    raise exception 'Este painel não está liberado para você.' using errcode = '42501';
  end if;
end;
$$;

create or replace function public.argos_tv_parear(p_pin text, p_nome text, p_contrato text, p_config jsonb default '{}'::jsonb)
returns uuid
language plpgsql
volatile
security definer
set search_path = public
as $$
declare
  v_id uuid;
begin
  perform public._argos_tv_exigir_contrato(p_contrato);
  update public.argos_tvs
     set status = 'aprovado', pin = null, expira_em = null,
         nome = coalesce(nullif(trim(p_nome), ''), 'TV'), contrato = p_contrato,
         config = public._argos_tv_config(p_config), aprovado_em = now(), aprovado_por = auth.uid()
   where status = 'pendente' and pin = regexp_replace(coalesce(p_pin, ''), '\D', '', 'g') and expira_em >= now()
  returning id into v_id;
  if v_id is null then
    raise exception 'PIN não encontrado ou expirado. Confira o número na tela da TV.' using errcode = 'P0002';
  end if;
  return v_id;
end;
$$;

-- TVs pareadas dos contratos do admin.
create or replace function public.argos_tv_listar()
returns table (id uuid, nome text, contrato text, config jsonb, user_agent text,
               aprovado_em timestamptz, aprovado_por_email text, ultimo_acesso_em timestamptz)
language plpgsql
stable
security definer
set search_path = public
as $$
begin
  perform public._argos_exigir_admin();
  return query
    select t.id, t.nome, t.contrato, t.config, t.user_agent, t.aprovado_em, a.email, t.ultimo_acesso_em
      from public.argos_tvs t
      left join public.argos_acessos a on a.user_id = t.aprovado_por
     where t.status = 'aprovado' and public.argos_tem_contrato(t.contrato)
     order by t.nome;
end;
$$;

create or replace function public.argos_tv_atualizar(p_id uuid, p_nome text, p_contrato text, p_config jsonb)
returns void
language plpgsql
volatile
security definer
set search_path = public
as $$
begin
  perform public._argos_tv_exigir_contrato((select contrato from public.argos_tvs where id = p_id and status = 'aprovado'));
  perform public._argos_tv_exigir_contrato(p_contrato);
  update public.argos_tvs
     set nome = coalesce(nullif(trim(p_nome), ''), nome), contrato = p_contrato, config = public._argos_tv_config(p_config)
   where id = p_id;
end;
$$;

create or replace function public.argos_tv_remover(p_id uuid)
returns void
language plpgsql
volatile
security definer
set search_path = public
as $$
begin
  perform public._argos_tv_exigir_contrato((select contrato from public.argos_tvs where id = p_id and status = 'aprovado'));
  delete from public.argos_tvs where id = p_id;
end;
$$;

-- ------------------------------------------------------------------
-- Permissões
-- ------------------------------------------------------------------
revoke all on function public._argos_tv_config(jsonb) from public, anon, authenticated;
revoke all on function public._argos_tv_exigir_contrato(text) from public, anon, authenticated;
revoke all on function public.argos_tv_iniciar(text, text) from public;
revoke all on function public.argos_tv_estado(text) from public;
revoke all on function public.argos_tv_parear(text, text, text, jsonb) from public, anon;
revoke all on function public.argos_tv_listar() from public, anon;
revoke all on function public.argos_tv_atualizar(uuid, text, text, jsonb) from public, anon;
revoke all on function public.argos_tv_remover(uuid) from public, anon;

grant execute on function public.argos_tv_iniciar(text, text) to anon;
grant execute on function public.argos_tv_estado(text) to anon;
grant execute on function public.argos_tv_parear(text, text, text, jsonb) to authenticated;
grant execute on function public.argos_tv_listar() to authenticated;
grant execute on function public.argos_tv_atualizar(uuid, text, text, jsonb) to authenticated;
grant execute on function public.argos_tv_remover(uuid) to authenticated;
