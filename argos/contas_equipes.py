"""Cria de uma vez as contas do painel /equipe para todas as equipes conhecidas.

As equipes saem do estado do bot do contrato: snapshot.json (ordens em
aberto e finalizadas dos últimos dias) e designacoes.json (equipe de cada
ordem na última exportação), mais os códigos passados na linha de comando.
Na SOC só entram códigos com o prefixo ARGOS_PREFIXO_EQUIPE_CAMPO. Na
Negociação o código é o nome da coluna "Equipe" em forma de slug
("Ceneged Angra 01" -> CENEGED-ANGRA-01); a lista impressa mostra o nome e o
código, que é o que a equipe digita no login.

Cada conta é criada como em web/api/equipe-cadastro.js (e-mail interno
<código>@ARGOS_DOMINIO_EQUIPES na SOC e neg-<código>@... na Negociação,
código e contrato em app_metadata), mas já
aprovada em argos_acessos, sem esperar o admin. Por padrão a equipe é
obrigada a trocar a senha no primeiro login (--sem-troca desliga). Equipes
que já têm conta são puladas, sem mexer na senha.

Precisa da chave service_role do Supabase, que não fica na VPS. Passe só
nesta execução:
    docker compose run --rm -e SUPABASE_SERVICE_ROLE_KEY=... argos \\
        python -m argos.contas_equipes --senha 'Senha01#'            (só lista)
    docker compose run --rm -e SUPABASE_SERVICE_ROLE_KEY=... argos \\
        python -m argos.contas_equipes --senha 'Senha01#' --confirmar
    (Negociação: acrescente --contrato negociacao)
"""

import argparse
import os
import re
import sys
from datetime import datetime, timezone

import requests

from argos import config
from argos.avisos_equipe import ARQUIVO_DESIGNACOES, normalizar_equipe
from argos.contratos import POR_ID, SOC, Contrato
from argos.estado import carregar_json
from argos.publicador import ARQUIVO_SNAPSHOT

# Tem que ser o mesmo do Vercel (web/api/_equipes.js).
DOMINIO_EQUIPES = os.environ.get("ARGOS_DOMINIO_EQUIPES") or "equipes.argos.local"


def codigo_valido(codigo: str, contrato: Contrato = SOC) -> bool:
    """Igual a codigoValido() em web/api/_equipes.js."""
    if contrato.equipe_por_nome:
        return bool(re.fullmatch(r"[A-Z0-9][A-Z0-9-]{1,59}", codigo))
    return bool(re.fullmatch(r"NI2[A-Z0-9-]{2,30}", codigo)) and codigo.startswith(config.PREFIXO_EQUIPE_CAMPO.upper())


def email_da_equipe(codigo: str, contrato: Contrato = SOC) -> str:
    """Igual a emailDaEquipe() em web/api/_equipes.js."""
    prefixo = "neg-" if contrato.id == "negociacao" else ""
    return f"{prefixo}{codigo.lower()}@{DOMINIO_EQUIPES}"


def equipes_conhecidas(extras: list[str], contrato: Contrato = SOC) -> dict[str, str]:
    """{código: nome} das equipes do contrato."""
    snapshot = carregar_json(contrato.arquivo(ARQUIVO_SNAPSHOT)) or {}
    campo = snapshot.get("campo") or {}
    secoes = [campo.get("registros"), campo.get("finalizadas"), (snapshot.get("programaveis") or {}).get("registros")]
    nomes: dict[str, str] = {}
    for secao in secoes:
        for r in secao or []:
            codigo = normalizar_equipe(r.get("equipe"))
            nomes.setdefault(codigo, r.get("equipe_nome") or codigo)
    for e in (carregar_json(contrato.arquivo(ARQUIVO_DESIGNACOES)) or {}).values():
        nomes.setdefault(normalizar_equipe(e), normalizar_equipe(e))
    for e in extras:
        codigo = contrato.chave_equipe(e)
        nomes.setdefault(codigo, e)
    return {c: nomes[c] for c in sorted(nomes) if codigo_valido(c, contrato)}


def _headers(chave: str) -> dict:
    return {"apikey": chave, "Authorization": f"Bearer {chave}", "Content-Type": "application/json"}


def equipes_com_conta(chave: str, contrato: Contrato = SOC) -> set[str]:
    r = requests.get(
        f"{config.SUPABASE_URL}/rest/v1/argos_acessos",
        params={"select": "equipe", "equipe": "not.is.null", "contrato": f"eq.{contrato.id}"},
        headers=_headers(chave), timeout=30,
    )
    r.raise_for_status()
    return {normalizar_equipe(l["equipe"]) for l in r.json()}


def criar_conta(chave: str, equipe: str, senha: str, trocar_senha: bool, contrato: Contrato = SOC,
                nome: str | None = None) -> str:
    """Retorna "criada", "existente" ou levanta RuntimeError."""
    email = email_da_equipe(equipe, contrato)
    r = requests.post(
        f"{config.SUPABASE_URL}/auth/v1/admin/users",
        json={
            "email": email,
            "password": senha,
            "email_confirm": True,
            "app_metadata": {"equipe": equipe, "contrato": contrato.id},
            "user_metadata": {"nome": nome or equipe},
        },
        headers=_headers(chave), timeout=30,
    )
    if r.status_code == 422 and re.search(r"already|exist|registered", r.text, re.I):
        return "existente"
    if not r.ok:
        raise RuntimeError(f"auth {r.status_code}: {r.text[:200]}")
    uid = r.json()["id"]

    a = requests.post(
        f"{config.SUPABASE_URL}/rest/v1/argos_acessos",
        params={"on_conflict": "user_id"},
        json={
            "user_id": uid,
            "email": email,
            "nome": nome or equipe,
            "status": "aprovado",
            "papel": "equipe",
            "equipe": equipe,
            "contrato": contrato.id,
            "decidido_em": datetime.now(timezone.utc).isoformat(),
            "trocar_senha": trocar_senha,
        },
        headers={**_headers(chave), "Prefer": "resolution=merge-duplicates,return=minimal"},
        timeout=30,
    )
    if not a.ok:
        # Sem a linha aprovada a conta ficaria pendente; desfaz para a próxima
        # execução tentar de novo do zero.
        requests.delete(f"{config.SUPABASE_URL}/auth/v1/admin/users/{uid}", headers=_headers(chave), timeout=30)
        raise RuntimeError(f"argos_acessos {a.status_code}: {a.text[:200]}")
    return "criada"


def main():
    p = argparse.ArgumentParser(description="Cria as contas do painel /equipe para todas as equipes conhecidas.")
    p.add_argument("--senha", required=True, help="senha inicial de todas as contas")
    p.add_argument("--confirmar", action="store_true", help="cria de fato (sem isto, só lista)")
    p.add_argument("--sem-troca", action="store_true", help="não obriga a trocar a senha no primeiro login")
    p.add_argument("--contrato", choices=sorted(POR_ID), default="soc")
    p.add_argument("equipes", nargs="*", help="códigos (ou nomes, na Negociação) extras, além dos do estado do bot")
    args = p.parse_args()
    contrato = POR_ID[args.contrato]

    chave = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not (config.SUPABASE_URL and chave):
        sys.exit("Defina SUPABASE_URL (no .env) e SUPABASE_SERVICE_ROLE_KEY (com -e, só nesta execução).")
    if len(args.senha) < 8:
        sys.exit("A senha precisa ter pelo menos 8 caracteres.")

    equipes = equipes_conhecidas(args.equipes, contrato)
    existentes = equipes_com_conta(chave, contrato)
    novas = [e for e in equipes if e not in existentes]
    print(f"[{contrato.nome}] {len(equipes)} equipe(s) conhecidas, {len(existentes & set(equipes))} já com conta, "
          f"{len(novas)} a criar:")
    for e in novas:
        print(f"  {e}" + (f"   ({equipes[e]})" if equipes[e] != e else ""))
    if not args.confirmar:
        print("Nada foi criado. Rode de novo com --confirmar.")
        return

    falhas = 0
    for e in novas:
        try:
            print(f"  {e}: {criar_conta(chave, e, args.senha, not args.sem_troca, contrato, equipes[e])}")
        except (RuntimeError, requests.RequestException) as erro:
            falhas += 1
            print(f"  {e}: FALHOU ({erro})")
    print(f"Pronto: {len(novas) - falhas} criada(s), {falhas} falha(s).")
    sys.exit(1 if falhas else 0)


if __name__ == "__main__":
    main()
