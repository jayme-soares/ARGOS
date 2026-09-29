"""ARGOS — painel único de acompanhamento de religações (eOrder / Enel Rio).

Dois monitores rodam em paralelo, cada um com seu próprio Chrome:
- Programáveis: a cada 1 min, busca as religações disponíveis para designar
  (Plano Diário > Atividades Programáveis) e avisa por push quando entram novas.
- Em campo: a cada 30 min, exporta a planilha da Busca TdC (filtro
  "PARCIAL RELIGA CENEGED - MARICÁ"), lê o vencimento (Prazo ANS Legal) de
  cada ordem em aberto e avisa por push quando uma ordem se aproxima do
  vencimento ou vence sem ser finalizada.

Os dois publicam um snapshot JSON no Upstash Redis, que o painel web (pasta
web/, hospedado no Vercel) lê para montar a tabela.
"""
