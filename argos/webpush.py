"""Notificação push pelo próprio painel (Web Push), além do ntfy.

Quem ativa o sino no painel vira uma inscrição no Supabase (ver
supabase/migrations/002_argos_push.sql). Aqui o bot busca as inscrições de
quem está aprovado e envia para cada uma, assinando com a chave VAPID.

Gerar as chaves (uma vez só; trocar a VAPID invalida todas as inscrições):
    python -m argos.webpush
"""

import base64
import json
import secrets

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from argos import config
from argos.log import log

# Por quanto tempo o serviço de push guarda um aviso para um celular
# desligado/sem rede. Depois disso o aviso já não serve (a ordem venceu ou
# chegou o próximo).
TTL_SEGUNDOS = 30 * 60


def _b64url(dados: bytes) -> str:
    return base64.urlsafe_b64encode(dados).rstrip(b"=").decode()


def _b64url_decode(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def chave_publica() -> str | None:
    """Chave pública VAPID (o painel precisa dela para inscrever o navegador),
    derivada da privada para as duas nunca ficarem trocadas."""
    if not config.VAPID_CHAVE_PRIVADA:
        return None
    try:
        privada = ec.derive_private_key(int.from_bytes(_b64url_decode(config.VAPID_CHAVE_PRIVADA), "big"), ec.SECP256R1())
    except Exception as e:
        log(f"ARGOS_VAPID_CHAVE_PRIVADA inválida: {e}", "NOTIFICAÇÃO")
        return None
    ponto = privada.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return _b64url(ponto)


def habilitado() -> bool:
    return bool(config.SUPABASE_URL and config.SUPABASE_ANON_KEY and config.PUSH_CHAVE and chave_publica())


def _rpc(funcao: str, corpo: dict):
    r = requests.post(
        f"{config.SUPABASE_URL}/rest/v1/rpc/{funcao}",
        headers={"apikey": config.SUPABASE_ANON_KEY, "Authorization": f"Bearer {config.SUPABASE_ANON_KEY}"},
        json=corpo,
        timeout=15,
    )
    if not r.ok:
        raise RuntimeError(f"Supabase {funcao} respondeu {r.status_code}: {r.text[:200]}")
    return r.json() if r.content else None


def url_painel(caminho: str = "") -> str:
    base = (config.PAINEL_URL or "").rstrip("/")
    return f"{base}/{caminho}" if base else f"/{caminho}"


def enviar(titulo: str, mensagem: str, prioridade: int = 3, equipe: str | None = None, tag: str | None = None) -> int:
    """Sem `equipe`: para a gestão (todos os aprovados que não são equipe).
    Com `equipe`: só para os aparelhos da conta daquela equipe, abrindo o
    painel /equipe. `tag` faz o aviso novo substituir o anterior de mesma tag.
    Nunca derruba o loop: falhas só vão para o log. Devolve quantos enviou."""
    from pywebpush import WebPushException, webpush

    try:
        if equipe:
            destinos = _rpc("argos_push_destinos_equipe", {"p_chave": config.PUSH_CHAVE, "p_equipe": equipe}) or []
        else:
            destinos = _rpc("argos_push_destinos", {"p_chave": config.PUSH_CHAVE}) or []
    except Exception as e:
        log(f"Falha ao buscar inscrições do painel: {e}", "NOTIFICAÇÃO")
        return 0

    dados = {"titulo": titulo, "mensagem": mensagem, "prioridade": prioridade,
             "url": url_painel("equipe") if equipe else (config.PAINEL_URL or "/")}
    if tag:
        dados["tag"] = tag
    payload = json.dumps(dados)
    expiradas, enviados = [], 0
    for d in destinos:
        try:
            webpush(
                subscription_info={"endpoint": d["endpoint"], "keys": {"p256dh": d["p256dh"], "auth": d["auth"]}},
                data=payload,
                vapid_private_key=config.VAPID_CHAVE_PRIVADA,
                vapid_claims={"sub": config.VAPID_CONTATO},
                ttl=TTL_SEGUNDOS,
                headers={"Urgency": "high" if prioridade >= 4 else "normal"},
                timeout=15,
            )
            enviados += 1
        except WebPushException as e:
            status = e.response.status_code if e.response is not None else None
            if status in (404, 410):  # navegador cancelou/limpou a inscrição
                expiradas.append(d["endpoint"])
            else:
                log(f"Falha no Web Push ({status}): {e}", "NOTIFICAÇÃO")
        except Exception as e:
            log(f"Falha no Web Push: {e}", "NOTIFICAÇÃO")

    if expiradas:
        try:
            _rpc("argos_push_remover", {"p_chave": config.PUSH_CHAVE, "p_endpoints": expiradas})
        except Exception as e:
            log(f"Falha ao remover inscrições expiradas: {e}", "NOTIFICAÇÃO")
    log(f"Web Push enviado para {enviados}/{len(destinos)} navegador(es)" + (f" da equipe {equipe}" if equipe else "")
        + (f", {len(expiradas)} inscrição(ões) expirada(s) removida(s)" if expiradas else "") + f": {titulo}", "NOTIFICAÇÃO")
    return enviados


def gerar_chaves():
    """Imprime as duas chaves novas e o SQL que cadastra a do bot."""
    privada = ec.generate_private_key(ec.SECP256R1())
    bruta = privada.private_numbers().private_value.to_bytes(32, "big")
    chave_bot = secrets.token_urlsafe(32)
    print("1) Coloque no .env da VPS (NÃO versione, NÃO compartilhe):\n")
    print(f"ARGOS_VAPID_CHAVE_PRIVADA={_b64url(bruta)}")
    print(f"ARGOS_PUSH_CHAVE={chave_bot}")
    print("\n2) Rode no SQL Editor do Supabase (depois do 002_argos_push.sql):\n")
    print("insert into public.argos_push_config (id, chave_hash)")
    print(f"values (1, encode(sha256(convert_to('{chave_bot}', 'UTF8')), 'hex'))")
    print("on conflict (id) do update set chave_hash = excluded.chave_hash;")


if __name__ == "__main__":
    gerar_chaves()
