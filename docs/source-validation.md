# Fase 0: validação de fontes

Atualizado em 16/11/2026. O código não assume que respostas de documentação significam cobertura para as rotas monitoradas.

## Melhores Destinos

- A página oficial “Melhores Destinos na rede” divulga um feed RSS FeedBurner: `https://feeds.feedburner.com/passagens-aereas`.
- Consulta pública feita em 16/11/2026 respondeu HTTP 200 com XML RSS e dez itens. O item mais recente observado era de 13/11/2026, mostrando que o cron de 30 minutos não garante publicação nova nessa frequência.
- O `robots.txt` do domínio principal bloqueia `/feed/` e caminhos `*/feed/`. O código consome somente o endereço FeedBurner divulgado oficialmente, nunca o caminho bloqueado, HTML de busca, CAPTCHA ou endpoints privados.
- O filtro usa correspondência de destino, não usa “Natal” como palavra-chave, pois Natal também é a origem NAT. Com o destino China inicial, aliases incluem China, Pequim/Beijing, Xangai/Shanghai e cidades configuradas.

Referências: [RSS divulgado pelo site](https://www.melhoresdestinos.com.br/melhores-destinos-na-rede), [robots.txt](https://www.melhoresdestinos.com.br/robots.txt).

## Travelpayouts Data API

- A documentação publica o endpoint `aviasales/v3/prices_for_dates`, que aceita aeroporto/cidade de origem e destino, mês/data de partida e retorno, moeda e token.
- São preços em cache de buscas de usuários; a documentação descreve registros encontrados nas últimas 48 horas para o endpoint por datas e retenção variável de 2–7 dias na API de dados. Resultados podem faltar para rotas pouco pesquisadas.
- A integração força ida e volta (`one_way=false`), solicita BRL e descarta duração fora de 14–30 dias. Como a API não recebe a composição de passageiros, o adaptador só usa esses preços no padrão de um adulto; configurações com outros passageiros são ignoradas. A resposta não comprova horários detalhados de escalas; esses valores nunca geram alerta imediato.
- Acesso requer cadastro e token. A validação ao vivo nas rotas NAT/REC–PEK/PVG está pendente. Antes do `source-check`, o operador precisa revisar os termos vigentes e definir `WATCHER_TRAVELPAYOUTS_TERMS_ACCEPTED=true`.

Referências: [Data API e parâmetros](https://support.travelpayouts.com/hc/en-us/articles/203956163-Aviasales-Data-API), [requisitos e cache](https://support.travelpayouts.com/hc/en-us/articles/203956083-Requirements-for-Aviasales-data-API-access), [limites por método](https://support.travelpayouts.com/hc/en-us/articles/4402565416594-API-rate-limits).

## SerpApi Google Flights

- A tabela de preços consultada em 16/11/2026 lista plano gratuito de 250 buscas/mês e 50/hora. O código reserva no máximo 200 buscas por mês e contabiliza chamadas detalhadas da ida e retorno como buscas separadas.
- A Account API é documentada como gratuita e mostra plano, cota restante e preço mensal. O código falha fechado se não verificar um plano com preço zero.
- O calendário documenta tipo ida e volta, janela de datas, duração, moeda e passageiros. A busca detalhada usa o `departure_token` para obter a volta da opção selecionada. O preço do calendário representa a viagem completa; as pernas não são somadas.
- Resultados em cache do SerpApi podem ser servidos por até uma hora sem consumir cota. Calendário sem detalhes fica marcado como incompleto. Uma tarifa só pode ser alertada quando uma busca detalhada recente retorna ambas as pernas e horários consistentes.
- SerpApi é um provedor terceirizado que agrega resultados do Google Flights, não uma API oficial do Google. Avalie os termos e a política de uso atuais da sua conta antes de ativá-la; defina `WATCHER_SERPAPI_TERMS_ACCEPTED=true`. A validação autenticada NAT/REC–China não foi realizada nesta implementação.
- O comando `source-check` limita a validação a oito calendários representativos e uma ida e volta detalhada (duas buscas, dez no total). Ele não usa plano pago nem habilita compra automática de créditos.

Referências: [planos](https://serpapi.com/pricing), [Google Flights](https://serpapi.com/google-flights-api), [Flights Calendar](https://serpapi.com/google-flights-calendar-api), [Account API](https://serpapi.com/account-api), [termos](https://serpapi.com/legal).

## Fontes não incluídas

- **Passagens Imperdíveis:** não foi confirmado um feed RSS/API pública apropriada. A tentativa de leitura pelo navegador de pesquisa foi recusada por política de robôs. Nenhum coletor HTML foi criado.
- **Voos Imperdíveis:** site público informa busca flexível iniciada pelo usuário; não foi encontrada API/feed pública para consulta automatizada. Nenhum coletor HTML foi criado.
- **Amadeus Self-Service:** o ambiente de teste não é evidência de preço real e produção tem franquia gratuita com cobrança por excesso. Excluída para cumprir o requisito de não gerar cobranças.

## Resultado e pendências

Feed RSS do Melhores Destinos é utilizável. As duas APIs de tarifas têm adaptadores implementados, mas **não foram aprovadas ao vivo**: o ambiente não continha credenciais. Elas permanecem bloqueadas no agendador até o `source-check` encontrar dados reais e gravar aprovação no estado JSON. Se as contas não retornarem preços ou as rotas não tiverem cobertura, a Fase 0 continua pendente e não há afirmação de que o sistema pode monitorar tarifas reais.
