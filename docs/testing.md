# Tutorial: testar e colocar o monitor em funcionamento

Execute os comandos na raiz do repositório. Os passos 1 e 2 não consultam APIs nem enviam mensagens. Os passos seguintes usam serviços reais e estão identificados abaixo.

## 1. Instalar

Se você já tem este repositório aberto, não precisa cloná-lo novamente. Em outra máquina:

```bash
git clone https://github.com/lucasboot/travel-deal-watcher.git
cd travel-deal-watcher
```

Em Linux/macOS com Python 3.12:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

Sempre ative `.venv` ao abrir outro terminal. Se o comando `travel-deal-watcher` não for encontrado, use `python -m travel_deal_watcher` com os mesmos argumentos.

## 2. Testar sem credenciais e sem consumir cotas

```bash
python -m pytest -q
python -m ruff check src tests
travel-deal-watcher --config config.yaml validate-config
travel-deal-watcher --config config.yaml plan
```

Resultados esperados: todos os testes passam, Ruff informa `All checks passed!`, a configuração retorna `Configuração válida.` e `plan` imprime as consultas planejadas em JSON. Os testes usam mocks. `plan` não executa nem reserva as consultas que mostra, e não comprova que as fontes têm preços para essas rotas.

## 3. Testar o feed real sem Telegram e sem APIs de tarifas

Este comando acessa apenas o RSS configurado:

```bash
TELEGRAM_BOT_TOKEN='' TELEGRAM_CHAT_IDS='' \
WATCHER_STATE_PATH=data/rss-test.json \
travel-deal-watcher --config config.yaml run --mode promotions
```

Espere `errors: 0` no resultado. `promotions: 0` é válido: o feed pode não conter uma publicação sobre a China. A primeira execução guarda as publicações existentes sem notificá-las. O arquivo `data/rss-test.json` contém o estado desse teste e fica ignorado pelo Git.

## 4. Configurar e testar o Telegram

Crie `.env` apenas se ainda não existir, preservando suas credenciais:

```bash
test -f .env || cp .env.example .env
```

Edite `.env` com seu token de bot e os IDs dos chats autorizados:

```dotenv
TELEGRAM_BOT_TOKEN=seu_token
TELEGRAM_CHAT_IDS=seu_chat_id
```

Para vários destinatários, separe os IDs por vírgula. Inicie uma conversa com seu bot antes do teste; para grupos, adicione o bot ao grupo e verifique que ele pode enviar mensagens. Confira também `telegram.enabled: true` no YAML. Não publique `.env` nem inclua os valores em issues ou logs.

O comando abaixo **envia um resumo real para todos os chats configurados**, mesmo se ainda não houver ofertas. Ele não consulta provedores de tarifas nem RSS:

```bash
WATCHER_STATE_PATH=data/telegram-test.json \
travel-deal-watcher --config config.yaml run --mode digest
```

Resultado esperado: a mensagem chega e `alerts: 1` indica que o resumo foi entregue a todos os destinatários. Sem credenciais, o texto aparece apenas no terminal e `alerts` fica zero. Repetir o comando com o mesmo estado no mesmo dia não deve duplicar o resumo. Para diagnosticar, consulte `deliveries` em `data/telegram-test.json`: `sent` confirma entrega, `failed` indica rejeição, `unknown`/`pending` exigem conferência manual, pois o monitor evita reenviar entregas incertas.

## 5. Validar tarifas reais com cota limitada

Revise [as fontes e limitações](source-validation.md). Obtenha uma conta/token da Travelpayouts Data API e/ou uma chave do plano gratuito SerpApi. Configure somente as fontes que pretende usar, sem ativar planos pagos ou compra automática de créditos.

Preencha em `.env` os tokens correspondentes:

```dotenv
TRAVELPAYOUTS_TOKEN=seu_token
SERPAPI_API_KEY=sua_chave
```

Após revisar os termos, acrescente, para cada fonte escolhida:

```dotenv
WATCHER_TRAVELPAYOUTS_TERMS_ACCEPTED=true
WATCHER_SERPAPI_TERMS_ACCEPTED=true
```

Uma fonte não utilizada pode ser desabilitada com `WATCHER_TRAVELPAYOUTS_ENABLED=false` ou `WATCHER_SERPAPI_ENABLED=false`. Mantenha as demais configurações em `config.yaml`.

```bash
travel-deal-watcher --config config.yaml run --mode source-check
```

**Este passo faz consultas reais.** A validação pode consumir até oito consultas Travelpayouts e até dez buscas SerpApi, sujeitas ao cache, à cota restante e ao orçamento mensal local. A SerpApi é bloqueada se o plano gratuito não puder ser confirmado. Não repita a validação em sequência para tentar forçar resultados.

Inspecione somente a seção de validação do estado:

```bash
python - <<'PY'
import json
from pathlib import Path

state = json.loads(Path('data/state.json').read_text())
print(json.dumps(state['validation'], indent=2, ensure_ascii=False))
PY
```

Cada fonte precisa de `passed: true`. `errors: 0` ou uma execução verde não significam que houve cobertura: a API pode ter retornado uma lista vazia. Leia `reason`, `usable_prices` ou `complete_round_trips`. Sem dados utilizáveis, a fonte permanece bloqueada. A Travelpayouts fornece preços indicativos e não dispara alertas imediatos de tarifa; esses alertas dependem de preços recentes e itinerários completos da SerpApi.

Depois da aprovação de uma fonte:

```bash
travel-deal-watcher --config config.yaml run --mode discovery
travel-deal-watcher --config config.yaml run --mode fares
travel-deal-watcher --config config.yaml run --mode digest
```

`discovery` consulta Travelpayouts; `fares` consulta SerpApi; ambos podem gastar cota. `digest` usa somente o estado salvo. Não receber alerta pode ser normal: orçamento, duração, escalas, deduplicação e disponibilidade dos dados precisam permitir a oferta. Não apague o estado de produção para contornar a cota ou a deduplicação.

## 6. Executar no GitHub Actions

Abra o repositório no GitHub e acesse **Settings → Secrets and variables → Actions**.

Em **Secrets**, cadastre os valores que usar: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_IDS`, `TRAVELPAYOUTS_TOKEN`, `SERPAPI_API_KEY`. `STATE_HASH_SECRET` é opcional, conforme o README. Nunca coloque tokens em Variables.

Em **Variables**, configure `WATCHER_TRAVELPAYOUTS_TERMS_ACCEPTED` e/ou `WATCHER_SERPAPI_TERMS_ACCEPTED` como `true` somente depois da revisão dos termos. Para fontes não utilizadas, configure a respectiva variável `*_ENABLED=false`. O orçamento SerpApi padrão é 200 buscas/mês; `WATCHER_SERPAPI_MONTHLY_BUDGET` pode reduzi-lo.

Na aba **Actions**:

1. Confira a execução **CI**: lint, testes e validação do YAML devem passar.
2. Abra **Monitor → Run workflow**, selecione a branch `main` e o modo `digest`. Confira a entrega no Telegram e o passo `Persist sanitized state`.
3. Rode **Monitor** com modo `source-check`. Confira os logs e `validation` em `data/state.json` na branch `watcher-state`.
4. Depois da validação, rode **Monitor** com modo `due`. Confira consultas, falhas e gravação do estado.
5. Acompanhe as próximas execuções agendadas na aba Actions. O cron desperta a cada 30 minutos; a aplicação aplica os intervalos de cada tarefa.

O estado local e o estado do Actions são independentes: validar localmente não libera automaticamente os provedores no GitHub. Evite manter os dois monitores ativos com a mesma chave de API; o controle local de consumo não é compartilhado entre eles.

Se o repositório for público, a branch `watcher-state` também expõe rotas, datas e preços pesquisados. Tokens e IDs de chat não fazem parte desse estado. Se o push dessa branch for bloqueado por regras do repositório, ajuste as permissões de escrita do workflow e as regras aplicáveis antes de deixar o monitor agendado funcionar.

## Como saber que terminou

O MVP estará validado operacionalmente quando os testes e a CI passarem, pelo menos uma fonte retornar dados reais utilizáveis, o resumo chegar ao Telegram e duas execuções do Actions preservarem o mesmo estado sem duplicar mensagens. Até esse teste com suas contas, a implementação dos adaptadores não é confirmação de cobertura das rotas.
