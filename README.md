# ARGOS

ARGOS é o painel único de acompanhamento das religações CENEGED Maricá no eOrder (Enel Rio).

```
VPS Hostinger (Docker)                          Vercel
┌───────────────────────────────┐               ┌──────────────────────┐
│ argos                         │   snapshot    │ web/  (painel)       │
│  ├─ monitor Programáveis (1m) │──── JSON ───▶ │  index.html + app.js │
│  └─ monitor Em campo    (30m) │   Upstash     │  api/snapshot.js     │
│        │ push                 │   Redis       │  (usuário/senha)     │
└────────┼──────────────────────┘               └──────────────────────┘
         ▼
      ntfy (celular / desktop)  +  Web Push (sino do painel)
```

## O que o ARGOS faz

**Programáveis** (a cada 1 min):
- Caminho no eOrder: Plano Diário → Atividades Programáveis → busca salva "CENEGED RELIGAÇÃO MARICA".
- Mostra no painel as religações disponíveis para designar, só do município de Maricá (a busca traz o Centro Operativo inteiro, com Niterói; o município sai do fim do endereço).
- Envia um push quando entra uma religação nova.

**Em campo** (a cada 30 min):
- Caminho no eOrder: Lista TdC → Busca TdC → filtro "PARCIAL RELIGA CENEGED - MARICÁ", com data de lançamento de 7 dias atrás até hoje.
- Exporta a planilha e lê estas colunas:
  - ordem (`Numero de Serviço`)
  - TdC
  - cliente
  - equipe (`Código Equipe`)
  - município
  - bairro
  - tipo (`Tipo de Serviço`)
  - vencimento (`Prazo ANS Legal`)
- Só entram as ordens do município de Maricá cujo `Código Equipe` começa com `NI2` (equipes da CENEGED).

**Pushes de vencimento** das ordens em campo. Os alertas são reavaliados a cada 5 min:
- 1h antes do vencimento
- 30 min antes do vencimento
- quando a ordem vence e continua em aberto
- um lembrete com as vencidas em aberto a cada 1h

Cada push lista só o TdC e a data/hora do vencimento; os detalhes ficam no painel.

Cada ordem recebe cada aviso uma única vez.

**Janela de funcionamento:** seg-sex, das 7h às 20h. Fora desse horário o Chrome fica desligado.

## Estrutura

| Caminho | Conteúdo |
|---|---|
| `argos/main.py` | Entrada: sobe o health-check, o watchdog e os dois monitores |
| `argos/workers.py` | Loops dos monitores: janela de horário, recuperação de falhas, modo sequencial |
| `argos/eorder/` | Selenium: driver, login, Programáveis, Busca TdC/exportação |
| `argos/planilha.py` | Leitura da planilha exportada. Detecta xlsx/xls/xlsb/HTML/XML pelo conteúdo |
| `argos/alertas.py` | Regras dos pushes de vencimento |
| `argos/notificacao.py` / `argos/webpush.py` | Envio dos pushes: ntfy e Web Push do painel |
| `argos/publicador.py` | Snapshot JSON: grava em `/data/snapshot.json` e publica no Upstash |
| `web/` | Painel (Vercel). HTML/CSS/JS puro, sem build |
| `tests/` | Testes da planilha e dos alertas |

O estado fica no volume `/data`: `programaveis.json`, `alertas.json`, `snapshot.json`, `downloads/` e `debug/`, onde ficam os screenshots de erro. Por isso um restart do container não repete pushes.

## Rodando localmente

```powershell
pip install -r requirements.txt
copy .env.example .env   # preencha
# carregue as variáveis do .env no terminal, por exemplo:
Get-Content .env -Encoding UTF8 | Where-Object { $_ -match '^\s*[^#].*=' } | ForEach-Object { $k,$v = $_ -split '=',2; Set-Item "env:$k" $v }

python -m unittest discover -s tests -t .      # testes

# Exportação da Busca TdC isolada, com o Chrome visível (bom para conferir seletores):
$env:ARGOS_HEADLESS = "0"
python -m argos.eorder.busca_tdc
python -m argos.planilha data\downloads\<arquivo>   # só ler uma planilha já baixada

python -m argos.main                               # o bot completo
```

## Deploy do bot na VPS (Docker)

```bash
git clone <repo> argos && cd argos
cp .env.example .env && nano .env
docker compose up -d --build
docker compose logs -f argos
```

Para atualizar: `git pull && docker compose up -d --build`. Os dados continuam no volume `argos-data`.

Se o container antigo do dani-bot ainda estiver rodando, pare-o antes com `docker stop <nome> && docker rm <nome>`. Assim não ficam dois bots logando com o mesmo usuário.

Diagnóstico:
- `docker compose exec argos python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8080').read().decode())"` mostra o status dos monitores.
- `docker compose cp argos:/data/debug ./debug` copia os screenshots de erro para fora do container.

## Upstash Redis

1. Crie um banco Redis grátis em https://console.upstash.com. Também dá para criar pelo Marketplace do Vercel: Storage → Upstash.
2. Copie a **REST URL** e o **REST Token**:
   - Na VPS (`.env`): `UPSTASH_REDIS_REST_URL` e `UPSTASH_REDIS_REST_TOKEN`. É o token de escrita.
   - No Vercel: as mesmas variáveis. Aqui pode ser o **read-only token**.

## Acesso ao painel (Supabase)

O login usa o Supabase Auth da empresa (email e senha). Quem já tem conta entra direto; quem não tem pode se cadastrar pela própria tela de login ("Não tem conta? Cadastre-se"). Em qualquer caso, o acesso ao ARGOS é controlado pela tabela `public.argos_acessos`:

- O **primeiro usuário** que entrar no painel vira administrador automaticamente.
- Os demais ficam **aguardando aprovação** até um admin aprovar na aba **Acessos**, que só aparece para admins.
- Admins podem aprovar, recusar, revogar e promover ou rebaixar outros admins. O último admin não pode ser removido.

Para configurar:

1. No Supabase da empresa, abra o **SQL Editor** e rode [`supabase/migrations/001_argos_acessos.sql`](supabase/migrations/001_argos_acessos.sql). A migração só cria a tabela `argos_acessos` e as funções `argos_*`; nada do que já existe é alterado.
2. Entre no painel com a sua conta **antes de divulgar o link**, porque o primeiro login vira admin.
3. Para o cadastro funcionar, em **Authentication → Sign In / Providers**, deixe ligado **Allow new users to sign up** (e o provedor Email).
4. Se **Confirm email** estiver ligado, a pessoa recebe um link e só consegue entrar depois de confirmar. Nesse caso, em **Authentication → URL Configuration**, adicione o endereço do painel no Vercel em **Redirect URLs** (e, se quiser, como **Site URL**).

## Painel no Vercel

1. Importe o repositório no Vercel e defina **Root Directory = `web`**. Framework: *Other*, sem build command.
2. Configure as variáveis de ambiente:
   - `UPSTASH_REDIS_REST_URL`
   - `UPSTASH_REDIS_REST_TOKEN`
   - `SUPABASE_URL`: em Project Settings → API
   - `SUPABASE_ANON_KEY`: a chave **anon/public**. Nunca use a service_role aqui.
3. Faça o deploy. Coloque a URL gerada em `ARGOS_PAINEL_URL` no `.env` da VPS, para que tocar no push abra o painel.

Para testar localmente: `cd web && npx vercel dev`, com as mesmas variáveis num `web/.env.local`.

## Notificações

Os avisos saem por dois canais ao mesmo tempo. Cada pessoa usa o que preferir.

**Pelo painel (sino no topo):** a pessoa entra no painel, toca no sino e aceita a permissão do navegador. Recebe mesmo com o painel fechado, sem instalar nada. Só recebe quem está aprovado; revogar o acesso corta os avisos, e sair do painel desativa as notificações naquele aparelho.
- Android e computador: Chrome, Edge ou Firefox.
- iPhone/iPad (iOS 16.4+): só com o ARGOS instalado. No Safari, Compartilhar → Adicionar à Tela de Início, abrir pelo ícone, entrar e tocar no sino.

**Pelo ntfy:** instale o app **ntfy** (Android, iOS ou desktop) e assine o tópico definido em `NTFY_TOPIC`. Quem souber o nome do tópico recebe os avisos, então use um sufixo aleatório e não divulgue.

### Configurar as notificações pelo painel (uma vez)

1. No SQL Editor do Supabase, rode [`supabase/migrations/002_argos_push.sql`](supabase/migrations/002_argos_push.sql).
2. Na VPS, gere as chaves: `docker compose run --rm argos python -m argos.webpush`. O comando imprime duas linhas para o `.env` e um `insert` para o Supabase.
3. Rode o `insert` no SQL Editor do Supabase.
4. No `.env` da VPS, preencha `SUPABASE_URL`, `SUPABASE_ANON_KEY` (os mesmos do Vercel), `ARGOS_PUSH_CHAVE`, `ARGOS_VAPID_CHAVE_PRIVADA` e `ARGOS_VAPID_CONTATO`, e suba de novo com `docker compose up -d`.

O Vercel não precisa de variável nova: a chave pública vai para o painel dentro do snapshot. Não troque a `ARGOS_VAPID_CHAVE_PRIVADA` depois: todos teriam que ativar o sino de novo.

## Configuração

Todas as opções estão comentadas em [`.env.example`](.env.example). As principais:

| Variável | Padrão | Uso |
|---|---|---|
| `ARGOS_FILTRO_CAMPO` | `PARCIAL RELIGA CENEGED - MARICÁ` | Filtro salvo da Busca TdC |
| `ARGOS_MUNICIPIO_CAMPO` | `MARICÁ` | Município das ordens em campo e das programáveis (sem diferenciar acento/caixa) |
| `ARGOS_PREFIXO_EQUIPE_CAMPO` | `NI2` | Prefixo do `Código Equipe` das equipes da CENEGED |
| `ARGOS_INTERVALO_CAMPO_MIN` | `30` | Intervalo entre exportações |
| `ARGOS_CAMPO_DIAS_ATRAS` | `7` | Data de lançamento: de N dias atrás até hoje |
| `ARGOS_ID_DATA_LANC_INICIO` / `_FIM` | `698246` / `698247` | IDs dos campos de data. Ajuste se o eOrder mudar |
| `ARGOS_ALERTAS_MIN` | `60,30` | Antecedências dos avisos |
| `ARGOS_MODO_SEQUENCIAL` | `0` | `1` se o eOrder não aceitar duas sessões simultâneas do mesmo usuário |

## Limitações conhecidas

- Programáveis: só a primeira página da grade (~25 linhas) é lida. O total vem do título "Lista Atividades (N)", e o painel avisa quando há mais linhas do que as exibidas.
- `ARGOS_DIAS_UTEIS_PRAZO` está em 2, que é temporário. A regra real é 1.
- Os seletores do menu de opções da Busca TdC e da lista de exportação são XPaths absolutos herdados do projeto Produção SOC e podem quebrar se o layout mudar. Quando algo falha, o screenshot vai para `/data/debug`.
