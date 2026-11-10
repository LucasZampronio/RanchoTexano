# Rancho Texano · Avaliações no Google

Dashboard das avaliações do Google Maps das lojas **Capão da Canoa**, **Ipiranga (Porto Alegre)** e **Novo Hamburgo**, com relatórios anual, mensal e diário. As avaliações abaixo de 4★ são classificadas por tema (tempo de espera, atendimento, comida, preço, ambiente, limpeza, delivery).

Uma GitHub Action coleta as avaliações novas todo dia às 7h, atualiza o `dados.json` e o GitHub Pages publica o dashboard atualizado.

## Estrutura

| Arquivo | O que é |
|---|---|
| `index.html` | O dashboard (abre pelo GitHub Pages) |
| `dados.json` | As avaliações coletadas. Só as abaixo de 4★ guardam o comentário |
| `lojas.json` | As lojas e o identificador de cada uma no Google Maps |
| `scripts/coletar.py` | Coleta, classifica os temas e atualiza o `dados.json` |
| `.github/workflows/atualizar.yml` | Roda o script todo dia |

## Configuração (uma vez só)

1. **Chave do SerpApi:** crie uma conta em [serpapi.com](https://serpapi.com) e copie a API key.
2. **Chave da API do Claude (opcional):** em [console.anthropic.com](https://console.anthropic.com). Sem ela, os temas são classificados por palavras-chave, que é menos preciso.
3. **Secrets do repositório:** em *Settings → Secrets and variables → Actions → New repository secret*, crie:
   - `SERPAPI_KEY`
   - `ANTHROPIC_API_KEY` (se tiver)
4. **GitHub Pages:** em *Settings → Pages*, escolha *Deploy from a branch*, branch `main`, pasta `/ (root)`.
5. **Limite de histórico (opcional):** para não buscar avaliações muito antigas, crie em *Settings → Secrets and variables → Actions → aba Variables* a variável `DATA_INICIO` (por exemplo `2025-01-01`).
6. **Primeira coleta:** em *Actions → Atualizar avaliações → Run workflow*, rode no modo `diario`.

Depois disso, tudo roda sozinho todo dia às 7h.

## Como funciona no plano gratuito do SerpApi (250 buscas/mês)

Cada página de avaliações conta como uma busca.

- **Avaliações novas:** todo dia, cerca de 1 busca por loja (~90 por mês).
- **Histórico:** as buscas que sobram no mês trazem as avaliações antigas, até 5 páginas (~100 avaliações) por dia, continuando de onde parou. O script sempre guarda uma reserva para a coleta diária até o fim do mês.
- O progresso fica em `estado.json`, e o dashboard avisa enquanto o histórico ainda está sendo carregado.
- **Quer o histórico de uma vez?** Assine um plano pago por um mês e rode a Action no modo `completo`.

## Observações

- O Google informa a data da última edição para avaliações editadas, então uma avaliação antiga editada pode aparecer com data recente.
- Se o script não encontrar uma loja pela busca, preencha o `data_id` dela no `lojas.json`. Ele aparece no link do Google Maps, no formato `0x...:0x...`.
