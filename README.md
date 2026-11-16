# Travel Deal Watcher

Monitor de tarifas internacionais e publicações de promoções. Roda localmente ou pelo GitHub Actions, mantém histórico JSON e envia alertas pelo Telegram.

**Comece pelo [tutorial de testes e ativação](docs/testing.md):** instalação, testes sem credenciais, RSS, envio de teste pelo Telegram, validação das APIs e execução manual no GitHub Actions, com os resultados esperados em cada etapa.

O projeto consulta fontes gratuitas e limita o uso da SerpApi ao plano de preço zero. As integrações de tarifas permanecem bloqueadas até que uma consulta autenticada encontre dados utilizáveis e os termos de uso correspondentes sejam aceitos na configuração. Preços de cache e resultados de busca não garantem disponibilidade para compra.

## Estado das fontes

| Fonte | Situação | Uso no projeto |
|---|---|---|
| Melhores Destinos RSS | Feed oficial testado; conteúdo pode estar atrasado | Publicações do destino configurado, identificadas como não verificadas |
| Travelpayouts Data API | Adaptador pronto; requer conta/token e teste das rotas | Preços indicativos em cache, sem verificar horários das conexões |
| SerpApi Google Flights | Adaptador pronto; requer conta gratuita, cota e revisão dos termos | Calendário e inspeção recente de ida e volta dentro das consultas gratuitas |
| Passagens Imperdíveis | Feed/API autorizado não confirmado | Não integrado |
| Voos Imperdíveis | Feed/API autorizado não confirmado | Não integrado |
| Amadeus Self-Service | Produção pode cobrar o excedente | Fora deste projeto |

As evidências, limites e pendências estão em [`docs/source-validation.md`](docs/source-validation.md). A validação autenticada de NAT/REC para a China ainda precisa ser feita com suas próprias contas; nenhum token foi fornecido ou usado durante a implementação.

## Requisitos e instalação

- Python 3.12 ou compatível.
- Git, para persistir o estado no workflow.
- Credenciais gratuitas opcionais das fontes e token de bot Telegram.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
```

Edite `config.yaml` para definir destinos, aeroportos, durações, limite de preço e frequências. O exemplo inicia com NAT e REC, destino China e aeroportos PEK, PKX, PVG, CAN e TFU. GRU e GIG ficam cadastrados como candidatos de reposicionamento; o MVP não soma trechos separados para eles.

Para uma viagem internacional com escala em qualquer país, configure destinos como país, cidade ou aeroporto e forneça seus códigos de chegada. `match_keywords` pode ser usado para associar publicações de RSS a nomes de destino que não apareçam como código IATA.

## Comandos

```bash
travel-deal-watcher --config config.yaml validate-config
travel-deal-watcher --config config.yaml plan
travel-deal-watcher --config config.yaml run --mode source-check
travel-deal-watcher --config config.yaml run --mode due
travel-deal-watcher --config config.yaml run --mode digest
```

`plan` não acessa a rede nem reserva consultas. `source-check` faz a validação autenticada e limitada da Fase 0. Para SerpApi, a checagem consulta o plano/cota, limita a validação a oito calendários e uma ida e volta detalhada (dez buscas no máximo), e só aprova a fonte se conseguir validar um itinerário completo. Travelpayouts testa até oito combinações NAT/REC, PEK/PVG e meses +3/+6; precisa encontrar ao menos um preço em cache. Resultados ausentes deixam a fonte bloqueada para o agendador, sem contornar o bloqueio nem repetir buscas em sequência.

O modo `due` executa apenas tarefas vencidas. `all` executa as tarefas configuradas, mas continua respeitando cache e cota. Modos específicos são úteis para depuração. Sem Telegram configurado, as mensagens aparecem no terminal e não são marcadas como entregues.

## Credenciais e parâmetros

Copie `.env.example` para `.env`. Não coloque credenciais em `config.yaml`.

| Variável | Uso |
|---|---|
| `TRAVELPAYOUTS_TOKEN` | Token da Data API |
| `SERPAPI_API_KEY` | Chave do plano gratuito SerpApi |
| `TELEGRAM_BOT_TOKEN` | Token do bot |
| `TELEGRAM_CHAT_IDS` | Um ou mais IDs separados por vírgula |
| `STATE_HASH_SECRET` | Chave opcional para pseudonimizar destinatários no JSON público |

Em GitHub, cadastre tokens e IDs em **Secrets**. Cadastre as variáveis operacionais em **Variables**. Variáveis aceitas pelo código:

`WATCHER_INTERESTING_PRICE_BRL`, `WATCHER_PROMOTIONS_INTERVAL_MINUTES`, `WATCHER_DISCOVERY_INTERVAL_HOURS`, `WATCHER_SERPAPI_CYCLES_PER_DAY`, `WATCHER_SERPAPI_MONTHLY_BUDGET`, `WATCHER_TP_MAX_QUERIES_PER_CYCLE`, `WATCHER_STATE_PATH`, `WATCHER_TRAVELPAYOUTS_ENABLED`, `WATCHER_SERPAPI_ENABLED`, `WATCHER_RSS_ENABLED`, `WATCHER_TRAVELPAYOUTS_TERMS_ACCEPTED` e `WATCHER_SERPAPI_TERMS_ACCEPTED`.

Os dois valores `*_TERMS_ACCEPTED` começam falsos. Revise os termos atuais de cada conta antes de defini-los como `true`. A integração SerpApi confere em toda execução que o plano é gratuito, que a cota existe e que o orçamento local ainda permite a consulta. A execução para antes de consultar se não conseguir verificar um plano de custo zero.

## GitHub Actions e estado

O workflow `Monitor` roda a cada 30 minutos; a aplicação limita as promoções a esse intervalo, a descoberta indicativa a 2 horas, a SerpApi a quatro ciclos por dia e o resumo a 8h em Fortaleza. O gatilho manual permite escolher o modo de execução. O agendamento usa runners padrão e não configura serviços pagos.

Ao tornar o repositório público, o estado fica numa branch `watcher-state`, separada da branch de código. Essa branch contém observações normalizadas, preços, consultas e entregas, sem tokens nem IDs de chat. Ela pode revelar rotas e datas pesquisadas; mantenha o repositório privado se esses dados forem sensíveis. O histórico é podado após o prazo configurado.

O workflow usa concorrência exclusiva para evitar escritas simultâneas no JSON. Para habilitar a execução:

1. Adicione os Secrets e Variables necessários.
2. Execute manualmente **Monitor → source-check**. O estado compartilhado registra quais provedores passaram.
3. Confira o resumo da execução e o arquivo de validação na branch `watcher-state`.
4. Execute `due` manualmente e confirme a prévia ou a entrega do Telegram.

O GitHub pode atrasar agendamentos e desativar cron de repositórios públicos sem atividade por 60 dias. Consulte a aba Actions periodicamente.

## Como os alertas funcionam

- Publicações do Melhores Destinos são encaminhadas como **publicadas, ainda não verificadas**. Itens antigos no primeiro carregamento apenas inicializam o estado.
- Preços da Travelpayouts são indicativos e não verificam janelas de conexão.
- A SerpApi procura datas em calendário, e detalha a ida e a volta antes de alertar uma tarifa.
- O alerta de tarifa exige preço até R$ 8.000 por pessoa, ida e volta, dados completos, no máximo duas conexões por sentido, escalas de 2–6 horas e no máximo 36 horas de deslocamento por sentido.
- Sem dados completos ou com custos de bagagem desconhecidos, a tarifa pode aparecer no resumo com as limitações sinalizadas.
- Uma nova notificação de preço exige queda de ao menos 5% e R$ 300 em relação ao último alerta do mesmo itinerário.
- Stopovers turísticos e bilhetes separados ainda não são compostos pelo MVP.

Uma interrupção de rede durante o envio ao Telegram pode deixar a entrega em estado incerto. O sistema registra o caso e não reenvia automaticamente essa mensagem, para evitar duplicatas.

## Desenvolvimento

```bash
python -m pytest
python -m ruff check src tests
```

Os testes usam fixtures e mocks; não consomem cota de provedores. `source-check` valida inicialmente as APIs de tarifas; depois da aprovação, os modos de monitoramento também consultam as fontes habilitadas. O RSS pode ser consultado sem credenciais. Veja o [tutorial de testes](docs/testing.md) para distinguir os comandos offline daqueles que fazem consultas ou enviam mensagens reais.
