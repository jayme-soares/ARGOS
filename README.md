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
- Envia pushes de vencimento das programáveis ainda não designadas (ver abaixo).

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
- Separa as ordens em aberto das finalizadas pela coluna `Estado TdC` (estados em `ARGOS_ESTADOS_FINALIZADOS`). Para isso o filtro salvo precisa incluir também os estados de finalizada/encerrada.
  - As em aberto alimentam a aba "Em campo" e os pushes de vencimento.
  - As finalizadas alimentam o indicador "Finalizadas hoje" (no prazo x fora do prazo), a aba "Finalizadas", a coluna da aba "Por equipe" e a aba "Finalizadas" do relatório Excel. Não geram push.
  - A hora de finalização é a maior `Data Fim` da aba `Linhas TdC` da planilha. Sem ela, vale a hora da primeira exportação em que a ordem apareceu finalizada.
  - Para ver os estados que vêm na planilha: `python -m argos.planilha <arquivo>`. O comando lista a contagem por `Estado TdC` e como cada um foi classificado.

**Pushes de vencimento**, em duas categorias: ordens em campo (reavaliadas a cada 5 min) e programáveis (a cada checagem de 1 min):
- 2h antes do vencimento
- 1h antes do vencimento
- 30 min antes do vencimento
- 15 min antes do vencimento
- quando a ordem vence e continua em aberto (em campo: "sem finalizar"; programável: "sem designar")
- só em campo: um lembrete com as vencidas em aberto a cada 1h

Se a ordem já aparece num nível mais urgente (por exemplo, entra faltando 20 min), só esse nível dispara. Os avisos de 30 e 15 min e o de vencida ficam na tela até a pessoa tocar.

Cada push lista só o TdC e a data/hora do vencimento; os detalhes ficam no painel.

Cada ordem recebe cada aviso uma única vez.

**Avisos às equipes de campo** (painel `/equipe`, ver [Painel das equipes](#painel-das-equipes-de-campo)):
- religa designada para a equipe (nova na exportação ou redesignada de outra equipe);
- religa perto de vencer (os mesmos níveis de `ARGOS_ALERTAS_MIN`) e religa vencida.

Cada equipe recebe só as próprias ordens, e cada aviso precisa ser confirmado no painel. Sem confirmação, o push é reenviado a cada `ARGOS_REENVIO_AVISO_MIN`.

**Janela de funcionamento:** seg-sex, das 7h às 20h. Fora desse horário o Chrome fica desligado.

## Estrutura

| Caminho | Conteúdo |
|---|---|
| `argos/main.py` | Entrada: sobe o health-check, o watchdog e os dois monitores |
| `argos/workers.py` | Loops dos monitores: janela de horário, recuperação de falhas, modo sequencial |
| `argos/eorder/` | Selenium: driver, login, Programáveis, Busca TdC/exportação |
| `argos/planilha.py` | Leitura da planilha exportada. Detecta xlsx/xls/xlsb/HTML/XML pelo conteúdo |
| `argos/alertas.py` | Regras dos pushes de vencimento |
| `argos/avisos_equipe.py` | Avisos às equipes: designação, vencimento, reenvio dos não confirmados |
| `argos/notificacao.py` / `argos/webpush.py` | Envio dos pushes: ntfy e Web Push do painel |
| `argos/publicador.py` | Snapshot JSON: grava em `/data/snapshot.json` e publica no Upstash |
| `web/` | Painel (Vercel). HTML/CSS/JS puro, sem build. `index.html` + `app.js` é o painel da gestão; `equipe.html` + `equipe.js` é o das equipes; `comum.js` é compartilhado |
| `tests/` | Testes da planilha e dos alertas |

O estado fica no volume `/data`: `programaveis.json`, `alertas.json` (em campo), `alertas_programaveis.json`, `designacoes.json` (equipe de cada ordem na última exportação), `snapshot.json`, `downloads/` e `debug/`, onde ficam os screenshots de erro. Por isso um restart do container não repete pushes.

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

## Painel das equipes de campo

Cada equipe (código `NI2...`) tem um acesso próprio em `/equipe` (por exemplo, `https://seu-projeto.vercel.app/equipe`). A tela foi feita para o celular.

- **Login:** código da equipe e senha. No primeiro acesso, a equipe toca em "Primeiro acesso? Cadastre a equipe", cria a senha e fica aguardando aprovação de um admin na aba **Acessos**. Um código tem uma conta só, que pode estar aberta em vários celulares ao mesmo tempo.
- **O que a equipe vê:** só as religas designadas para ela. As em aberto aparecem com contagem regressiva, cor de urgência, endereço e link para o mapa. As finalizadas (hoje ou nos últimos 7 dias) aparecem com "no prazo" ou "fora do prazo". O filtro por equipe é feito no servidor (`/api/snapshot`), então uma equipe nunca recebe dados de outra.
- **Notificações:** a equipe ativa o sino (ou a faixa "Ative as notificações") em cada celular. Recebe só os avisos das próprias ordens, e nunca os da gestão. A gestão também não recebe os das equipes.
- **Confirmação obrigatória:** cada aviso fica pendente até a equipe tocar em "Confirmo que visualizei". Enquanto houver pendentes, o painel fica bloqueado por esse aviso e o push é reenviado a cada `ARGOS_REENVIO_AVISO_MIN`. Se a ordem sai da equipe (foi finalizada ou redesignada), o aviso é dispensado.
- **Senha esquecida:** na tela de login, "Esqueci a senha" registra o pedido, que aparece em destaque na aba **Acessos**. O admin clica em "Redefinir senha" e repassa a senha temporária mostrada. A equipe é obrigada a criar uma senha nova no próximo login. "Excluir conta" libera o código para um novo cadastro.
- **No painel da gestão:**
  - O indicador **Avisos pendentes** e a aba **Avisos** mostram cada aviso com equipe, tipo, TdC, envio, status (pendente, confirmado ou dispensado), hora da confirmação, tempo até confirmar e reenvios. A aba também vai para o Excel.
  - A aba **Por equipe** mostra a **última visualização** do painel por cada equipe (resolução de 1 min), os avisos pendentes e quantos celulares têm push ativo.

Para configurar (uma vez):

1. No SQL Editor do Supabase, rode [`supabase/migrations/003_argos_equipes.sql`](supabase/migrations/003_argos_equipes.sql), depois da 001 e da 002.
2. No Vercel, adicione a variável `SUPABASE_SERVICE_ROLE_KEY` (Project Settings → API do Supabase → **service_role**). Ela fica só nas funções `/api/equipe-cadastro` e `/api/admin-equipe`, que criam as contas das equipes, trocam a senha e excluem a conta. Ela nunca vai para o navegador. Faça o redeploy.
3. Opcional: `ARGOS_DOMINIO_EQUIPES` no Vercel (padrão `equipes.argos.local`). As contas das equipes usam um e-mail interno `<código>@<domínio>`, que a equipe não precisa saber. Não troque depois de criar contas.
4. A VPS não precisa de variável nova. Basta atualizar o bot (`git pull` + `docker compose up -d --build`). Os avisos usam o mesmo Supabase e as mesmas chaves do Web Push.
5. Teste: com uma equipe aprovada e o sino ativo no celular, rode na VPS `docker compose run --rm argos python -m argos.avisos_equipe --teste NI2XXXX`. O aviso de teste chega, bloqueia o painel da equipe até ela confirmar e aparece na aba **Avisos**.

Na primeira exportação depois da atualização, o bot só registra a equipe de cada ordem. Os avisos de designação começam na exportação seguinte, para que todas as ordens já abertas não cheguem como "novas".

## Painel no Vercel

1. Importe o repositório no Vercel e defina **Root Directory = `web`**. Framework: *Other*, sem build command.
2. Configure as variáveis de ambiente:
   - `UPSTASH_REDIS_REST_URL`
   - `UPSTASH_REDIS_REST_TOKEN`
   - `SUPABASE_URL`: em Project Settings → API
   - `SUPABASE_ANON_KEY`: a chave **anon/public**.
   - `SUPABASE_SERVICE_ROLE_KEY`: a chave **service_role**, só para as contas das equipes (ver acima). É usada apenas no servidor, nas funções `/api/*`.
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
| `ARGOS_ESTADOS_FINALIZADOS` | `Finalizado,Encerrado,Concluído` | Valores de `Estado TdC` que contam como finalizada (sem diferenciar acento/caixa, por prefixo) |
| `ARGOS_INTERVALO_CAMPO_MIN` | `30` | Intervalo entre exportações |
| `ARGOS_CAMPO_DIAS_ATRAS` | `7` | Data de lançamento: de N dias atrás até hoje |
| `ARGOS_ID_DATA_LANC_INICIO` / `_FIM` | `698246` / `698247` | IDs dos campos de data. Ajuste se o eOrder mudar |
| `ARGOS_ALERTAS_MIN` | `120,60,30,15` | Antecedências dos avisos de vencimento (em campo e programáveis) |
| `ARGOS_AVISOS_EQUIPE` | `1` | Avisos às equipes de campo (painel `/equipe`) |
| `ARGOS_REENVIO_AVISO_MIN` | `10` | Reenvio do push enquanto a equipe não confirma o aviso |
| `ARGOS_MODO_SEQUENCIAL` | `0` | `1` se o eOrder não aceitar duas sessões simultâneas do mesmo usuário |

## Limitações conhecidas

- Programáveis: só a primeira página da grade (~25 linhas) é lida. O total vem do título "Lista Atividades (N)", e o painel avisa quando há mais linhas do que as exibidas.
- Finalizadas: só aparecem as ordens com data de lançamento dentro da janela da Busca TdC (`ARGOS_CAMPO_DIAS_ATRAS`) e são atualizadas a cada exportação (30 min). Canceladas não contam como finalizadas, a menos que o estado esteja em `ARGOS_ESTADOS_FINALIZADOS`.
- Avisos às equipes: as designações são vistas a cada exportação (30 min), então o aviso de religa designada pode chegar até 30 min depois da designação no eOrder. As notificações dependem de cada celular ter ativado o sino. No iPhone, o ARGOS precisa estar instalado na Tela de Início.
- `ARGOS_DIAS_UTEIS_PRAZO` está em 2, que é temporário. A regra real é 1.
- Os seletores do menu de opções da Busca TdC e da lista de exportação são XPaths absolutos herdados do projeto Produção SOC e podem quebrar se o layout mudar. Quando algo falha, o screenshot vai para `/data/debug`.
