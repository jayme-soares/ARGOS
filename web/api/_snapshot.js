// Leitura do snapshot no Upstash, compartilhada por snapshot.js e tv.js
// (arquivos com "_" não viram rota no Vercel).
//
// O bot grava duas chaves por contrato (ver argos/publicador.py), em
// gzip+base64 ("gz:..."):
//   <chave>        tudo menos o campo, com "campo_hash"   (~3 KB, muda a cada minuto)
//   <chave>:campo  "<hash>:gz:..." com a seção campo      (~250 KB, muda a cada 30 min)
//
// Para caber na banda do plano gratuito do Upstash, a instância da função
// guarda em memória o que leu: a chave principal por CACHE_MS (todos os
// painéis que perguntarem nesse intervalo dividem uma leitura) e o campo
// enquanto o hash dele não mudar. No Vercel a mesma instância atende muitas
// requisições seguidas, então o campo sai do Upstash ~1 vez por exportação.
import { gunzipSync } from "node:zlib";

const KEY = process.env.ARGOS_SNAPSHOT_KEY || "argos:snapshot";
// Iguais a Contrato.snapshot_key em argos/contratos.py.
export const CHAVES = { soc: KEY, negociacao: `${KEY}:negociacao` };

const CACHE_MS = 20_000;
const cacheBase = new Map();   // contrato -> { em, promessa }
const cacheCampo = new Map();  // contrato -> { hash, campo }

function decodificar(valor) {
  if (valor.startsWith("gz:")) {
    return JSON.parse(gunzipSync(Buffer.from(valor.slice(3), "base64")).toString("utf8"));
  }
  return JSON.parse(valor);
}

async function get(chave) {
  const r = await fetch(`${process.env.UPSTASH_REDIS_REST_URL}/get/${encodeURIComponent(chave)}`, {
    headers: { Authorization: `Bearer ${process.env.UPSTASH_REDIS_REST_TOKEN}` },
  });
  if (!r.ok) throw new Error(`Upstash respondeu ${r.status}.`);
  return (await r.json()).result;
}

async function lerCampo(contrato, hash) {
  const atual = cacheCampo.get(contrato);
  if (atual && atual.hash === hash) return atual.campo;
  const valor = await get(`${CHAVES[contrato]}:campo`);
  if (!valor) return null;
  const sep = valor.indexOf(":");
  const campo = decodificar(valor.slice(sep + 1));
  // Guarda pelo hash que veio junto do valor: se o bot regravou entre as duas
  // leituras, a próxima requisição acerta sozinha.
  cacheCampo.set(contrato, { hash: valor.slice(0, sep), campo });
  return campo;
}

async function lerDoUpstash(contrato) {
  const valor = await get(CHAVES[contrato]);
  if (!valor) return null;
  const snapshot = decodificar(valor);
  if (snapshot.campo_hash) {
    snapshot.campo = await lerCampo(contrato, snapshot.campo_hash);
    delete snapshot.campo_hash;
  }
  return snapshot;
}

// Snapshot completo do contrato (ou null se o bot ainda não publicou). O
// objeto é compartilhado entre requisições: não altere, só leia.
export function lerSnapshot(contrato) {
  const agora = Date.now();
  const atual = cacheBase.get(contrato);
  if (atual && agora - atual.em < CACHE_MS) return atual.promessa;
  const promessa = lerDoUpstash(contrato);
  cacheBase.set(contrato, { em: agora, promessa });
  // Erro não fica em cache: a próxima requisição tenta de novo.
  promessa.catch(() => { if (cacheBase.get(contrato)?.promessa === promessa) cacheBase.delete(contrato); });
  return promessa;
}
