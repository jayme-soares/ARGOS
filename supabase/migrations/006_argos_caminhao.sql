-- ARGOS — religas que precisam de caminhão.
--
-- A maioria das equipes usa carro. Quando a equipe chega e vê que a religa
-- precisa de caminhão, marca a ordem no painel /equipe (antes era só um
-- WhatsApp aos admins). A marcação fica registrada aqui e aparece no painel
-- da gestão enquanto a ordem estiver em campo e também quando ela voltar para
-- as programáveis, para não ser mandada de novo a uma equipe de carro.
--
-- - A marcação é da ordem (Numero de Serviço), por contrato; o TdC vai junto
--   porque a ordem pode voltar com outro TdC. Uma marcação ativa por ordem.
-- - Quem marca: a equipe (só no próprio contrato) ou a gestão do contrato
--   (ex.: recebeu pelo WhatsApp). Quem desmarca: a equipe que marcou ou a
--   gestão. Desmarcar não apaga: guarda quem e quando (histórico).
-- - Equipes e gestão do contrato leem as marcações ativas do contrato (não
--   há dado de cliente aqui).
--
-- Depende de 001, 003 e 004 (independe da 005, das TVs).

create table if not exists public.argos_caminhao (
  id              bigserial primary key,
  contrato        text not null check (contrato in ('soc', 'negociacao')),
  ordem           text not null,
  tdc             text,
  equipe          text,            -- equipe que marcou (null = gestão)
  observacao      text,
  marcado_em      timestamptz not null default now(),
  marcado_por     uuid references auth.users (id) on delete set null,
  marcado_por_nome text,
  removido_em     timestamptz,
  removido_por    uuid references auth.users (id) on delete set null,
  removido_por_nome text
);
create unique index if not exists argos_caminhao_ativa on public.argos_caminhao (contrato, ordem) where removido_em is null;
create index if not exists argos_caminhao_contrato on public.argos_caminhao (contrato, marcado_em desc);

alter table public.argos_caminhao enable row level security;
revoke all on table public.argos_caminhao from anon;
revoke all on table public.argos_caminhao from authenticated;
grant select on table public.argos_caminhao to authenticated;

drop policy if exists argos_caminhao_select on public.argos_caminhao;
create policy argos_caminhao_select on public.argos_caminhao
  for select to authenticated
  using (public.argos_tem_contrato(contrato) or contrato = public.argos_meu_contrato_equipe());

-- Marca a ordem como "precisa de caminhão". A equipe marca no próprio
-- contrato (p_contrato é ignorado); a gestão informa o contrato. Se a ordem
-- já está marcada, devolve a marcação existente (só completa o TdC e a
-- observação que faltarem).
create or replace function public.argos_caminhao_marcar(
  p_ordem text, p_tdc text default null, p_observacao text default null, p_contrato text default null
)
returns public.argos_caminhao
language plpgsql
volatile
security definer
set search_path = public
as $$
declare
  v_equipe text := public.argos_minha_equipe();
  v_contrato text;
  v_nome text;
  v_ordem text := nullif(trim(p_ordem), '');
  v_tdc text := nullif(trim(p_tdc), '');
  v_obs text := nullif(left(trim(p_observacao), 500), '');
  v_linha public.argos_caminhao;
begin
  if v_equipe is not null then
    v_contrato := public.argos_meu_contrato_equipe();
  elsif public.argos_tem_contrato(p_contrato) then
    v_contrato := p_contrato;
  else
    raise exception 'Sem acesso para marcar ordens deste contrato.' using errcode = '42501';
  end if;
  v_ordem := coalesce(v_ordem, v_tdc);
  if v_ordem is null then
    raise exception 'Informe a ordem.' using errcode = '22023';
  end if;
  select coalesce(nome, equipe, email) into v_nome from public.argos_acessos where user_id = auth.uid();

  perform pg_advisory_xact_lock(hashtext('argos_caminhao:' || v_contrato || ':' || v_ordem));
  update public.argos_caminhao
     set tdc = coalesce(tdc, v_tdc), observacao = coalesce(observacao, v_obs)
   where contrato = v_contrato and ordem = v_ordem and removido_em is null
  returning * into v_linha;
  if found then
    return v_linha;
  end if;

  insert into public.argos_caminhao (contrato, ordem, tdc, equipe, observacao, marcado_por, marcado_por_nome)
  values (v_contrato, v_ordem, v_tdc, v_equipe, v_obs, auth.uid(), v_nome)
  returning * into v_linha;
  return v_linha;
end;
$$;

-- Tira a marcação: a equipe só as que ela marcou; a gestão, qualquer uma do contrato.
create or replace function public.argos_caminhao_desmarcar(p_id bigint)
returns public.argos_caminhao
language plpgsql
volatile
security definer
set search_path = public
as $$
declare
  v_equipe text := public.argos_minha_equipe();
  v_linha public.argos_caminhao;
  v_nome text;
begin
  select * into v_linha from public.argos_caminhao where id = p_id and removido_em is null;
  if not found then
    raise exception 'Marcação não encontrada.' using errcode = 'P0002';
  end if;
  if v_equipe is not null then
    if v_linha.contrato is distinct from public.argos_meu_contrato_equipe() or v_linha.equipe is distinct from v_equipe then
      raise exception 'Só a equipe que marcou ou a coordenação pode desmarcar.' using errcode = '42501';
    end if;
  elsif not public.argos_tem_contrato(v_linha.contrato) then
    raise exception 'Sem acesso a este contrato.' using errcode = '42501';
  end if;
  select coalesce(nome, equipe, email) into v_nome from public.argos_acessos where user_id = auth.uid();

  update public.argos_caminhao
     set removido_em = now(), removido_por = auth.uid(), removido_por_nome = v_nome
   where id = p_id
  returning * into v_linha;
  return v_linha;
end;
$$;

revoke all on function public.argos_caminhao_marcar(text, text, text, text) from public, anon;
revoke all on function public.argos_caminhao_desmarcar(bigint) from public, anon;
grant execute on function public.argos_caminhao_marcar(text, text, text, text) to authenticated;
grant execute on function public.argos_caminhao_desmarcar(bigint) to authenticated;
