# A Ponte: um agente A2A com MCP por dentro

Entrega do desafio do MBA Engenharia de Software com IA (curso de MCP e A2A).

Dois processos separados, em Python:

| Processo | Porta | Endpoints | Código |
|---|---|---|---|
| Servidor MCP (SDK oficial `mcp==2.3.0`, spec `2026-07-28`, Streamable HTTP) | `7301` | `/mcp` | [servidor-mcp/](servidor-mcp/) |
| Agente: servidor A2A v1.0 (JSON-RPC) por fora, host MCP por dentro | `7300` | `/.well-known/agent-card.json`, `/a2a` | [agente/](agente/) |

O agente não usa LLM. Ele interpreta o pedido em formato fixo e decide por regra.

## Como rodar

Pré-requisitos: Python 3.11 ou superior e [uv](https://docs.astral.sh/uv/). As versões estão travadas no `pyproject.toml` e no `uv.lock`.

```bash
git clone https://github.com/Maicon93/mba-testes-a2a-with-mcp.git
cd mba-testes-a2a-with-mcp
uv sync
```

**Terminal 1, servidor MCP.** A chave de integridade do `requestState` vem de `REQUEST_STATE_SECRET` e precisa ter no mínimo 32 bytes aleatórios. Gere a sua (nunca commite o valor):

```bash
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
./subir-mcp.sh
```

O stderr desse terminal é o log de requests. Cada linha tem `method`, `id`, `name`, `traceparent` e `retry=sim` quando o request traz `requestState`.

**Terminal 2, agente:**

```bash
./subir-agente.sh
```

**Terminal 3, validador** (com os dois processos recém-iniciados):

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Para conferir que o `requestState` sobrevive a um restart, reinicie o servidor MCP **com o mesmo `REQUEST_STATE_SECRET` exportado**. O estado viaja no token, então a chave precisa ser a mesma.

Variáveis opcionais, todas com os valores padrão do enunciado: `MCP_PORT` (7301), `AGENTE_PORT` (7300), `MCP_URL` (`http://127.0.0.1:7301/mcp`) e `AGENTE_URL` (`http://127.0.0.1:7300`, usada no card).

Sem `uv`: `python3 -m venv .venv && .venv/bin/pip install mcp==2.3.0 httpx==0.28.1 starlette==1.10.0 uvicorn==0.54.0`. Depois rode `python servidor.py` dentro de `servidor-mcp/` e `python agente.py` dentro de `agente/`.

## Onde a ponte acontece

**Ida.** Quando o servidor MCP encontra conflito, o resolver `escolha_de_sala` ([servidor-mcp/servidor.py:181](servidor-mcp/servidor.py#L181)) devolve um `Elicit`. O SDK então encerra o `tools/call` com `resultType: input_required`, uma elicitation em form mode e o `requestState` selado. Do lado do agente, isso chega em `_aplicar_resultado` ([agente/agente.py:130](agente/agente.py#L130)). Ali o `input_required` vira `TASK_STATE_INPUT_REQUIRED`, com a linha `alternativas: ...` na ordem do `enum`. O `requestState`, a chave do `inputRequests` e o request original ficam guardados num objeto `Pendencia` ligado àquela Task ([agente/tarefas.py](agente/tarefas.py)). Esse objeto nunca é serializado no wire A2A.

**Volta.** Quando chega o `SendMessage` com `taskId` e `escolha=<id>`, o agente executa `_continuar` ([agente/agente.py:192](agente/agente.py#L192)). Ele repete o mesmo `tools/call`, com um **id JSON-RPC novo** (todo request do cliente gera um id aleatório em [agente/mcp_cliente.py](agente/mcp_cliente.py)). O retry leva o `inputResponses` sob a mesma chave e o `requestState` ecoado byte a byte. `escolha=recusar` vira `action: decline` e a Task termina em `TASK_STATE_CANCELED`. Uma escolha fora do `enum` mantém a Task pausada e repete a lista.

## Decisões técnicas

- **Proteção do `requestState`.** Uso o `RequestStateSecurity(keys=[REQUEST_STATE_SECRET])` do próprio SDK. Ele aplica AES-256-GCM, com a chave derivada por HKDF-SHA256, o que garante integridade e confidencialidade. O envelope selado carrega a expiração, a *audience* (o nome do servidor) e o vínculo com o request: a tool e um digest SHA-256 dos argumentos. Um token adulterado, expirado ou apresentado com argumentos diferentes dos selados é rejeitado com `-32602` (`Invalid or expired requestState`). O SDK recusa chaves com menos de 32 bytes, e o servidor não sobe sem `REQUEST_STATE_SECRET`.
- **Validade.** O token vale 10 minutos (`TTL_REQUEST_STATE = 600`).
- **Nenhum estado no servidor entre as rodadas.** O resolver roda de novo no retry e a resposta do cliente só é aceita se a pergunta renderizar igual à que foi selada no token. Por isso o retry funciona depois de reiniciar o processo, desde que a chave seja a mesma. As reservas ficam em memória, como o enunciado permite.
- **Capability e `-32021`.** O SDK verifica `elicitation.form` no `_meta` de cada request antes de emitir a elicitation. Também é ele que recusa `_meta` incompleto (`-32602`, HTTP 400) e headers divergentes do corpo (`-32020`).
- **Estado das Tasks.** Fica em memória no agente, num dicionário `task_id -> Tarefa` ([agente/tarefas.py](agente/tarefas.py)). Cada Task tem um `asyncio.Lock`, para que duas continuações simultâneas não se misturem. O MRTR pendente é por Task, então duas Tasks pausadas ao mesmo tempo nunca trocam de `requestState`. Estado terminal é definitivo: um `SendMessage` para uma Task `COMPLETED`, `CANCELED` ou `FAILED` recebe o erro A2A `-32004` (UnsupportedOperation), e um `taskId` desconhecido recebe `-32001`.
- **Cliente MCP próprio no agente, em vez do `mcp.client.Client`.** O cliente do SDK declara `elicitation` com `form` **e** `url`, carimba o próprio `_meta` por cima do que o chamador envia e, com um callback de elicitation configurado, responde à pergunta sozinho. Com isso a Task nunca pausaria. Por isso o agente usa um cliente HTTP enxuto ([agente/mcp_cliente.py](agente/mcp_cliente.py)). Ele envia em todo request `protocolVersion`, `clientInfo` e `clientCapabilities: {"elicitation": {"form": {}}}` no `_meta`, os headers `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name`, e enxerga o `input_required` cru.
- **Descoberta em runtime.** Para cada Task nova, o agente faz `tools/list` e confere se `reservar_sala` existe. Os argumentos são montados a partir do `inputSchema` descoberto, não de uma lista fixa. Depois o agente lê `politica://uso` e extrai a versão da primeira linha. É essa versão que vai no campo `politica` do artifact.
- **`traceparent`.** O trace-id do header `traceparent` da chamada A2A fica gravado na Task. Cada request MCP daquela Task, inclusive o retry, leva esse mesmo trace-id com um span-id novo no `_meta.traceparent`. O servidor MCP registra o valor no stderr.
- **O agente não implementa regra de sala.** Validação de política, conflito e cálculo de alternativas ficam todos no servidor MCP. O agente só traduz protocolo.

## Saída do validador

Última execução, com os dois processos recém-iniciados pelos scripts acima:

```
trace-id desta execucao: 7c8b36c5b2f0c4ba1e82d198dfc66d27
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```

Trecho do stderr do servidor MCP na mesma execução. Ele mostra o `tools/list` antes do primeiro `tools/call` do agente, o trace-id do validador propagado e o retry com id diferente do request inicial:

```
[mcp] method=tools/list id=agente-bfddbd1fa328 traceparent=00-7c8b36c5b2f0c4ba1e82d198dfc66d27-2e5c30b924981451-01
[mcp] method=resources/read id=agente-0ca63e809155 name=politica://uso traceparent=00-7c8b36c5b2f0c4ba1e82d198dfc66d27-cc7a01459262c70a-01
[mcp] method=tools/call id=agente-d40eceb89a18 name=reservar_sala traceparent=00-7c8b36c5b2f0c4ba1e82d198dfc66d27-5312cdc216d7ab89-01
[mcp] method=tools/list id=agente-5bc00365cb34 traceparent=00-7c8b36c5b2f0c4ba1e82d198dfc66d27-f53f960bb14a9723-01
[mcp] method=resources/read id=agente-45412bf2da42 name=politica://uso traceparent=00-7c8b36c5b2f0c4ba1e82d198dfc66d27-a90b927fe2c9fbc9-01
[mcp] method=tools/call id=agente-7dd6605175af name=reservar_sala traceparent=00-7c8b36c5b2f0c4ba1e82d198dfc66d27-361885d1fb374b3e-01
[mcp] method=tools/call id=agente-9a1e76f4fc1d name=reservar_sala traceparent=00-7c8b36c5b2f0c4ba1e82d198dfc66d27-03a659c61d336c44-01 retry=sim
```
