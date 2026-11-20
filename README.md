# TaskMind Logistics

Núcleo de decisão para entregas: prioriza pedidos, valida dependências, calcula rotas, distribui pedidos,
garante integridade/idempotência em SQLite e replaneja quando eventos chegam. O núcleo usa **somente a
biblioteca padrão**; FastAPI é opcional.

## Como executar

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # pytest (+ fastapi/uvicorn/httpx se quiser a API)
pytest                                   # suíte completa (tests/test_api.py é pulado sem FastAPI)
python -m app.cli --time 100             # demo sobre data/*.json, explicando cada decisão
python benchmarks/stress.py              # teste de estresse (tempo e memória)
uvicorn app.api:create_app --factory     # API REST (opcional)
```

Configuração por variáveis de ambiente (`app/config.py`): `TASKMIND_DB_PATH`, `TASKMIND_DATA_DIR`,
`TASKMIND_LOG_LEVEL`, `TASKMIND_LOG_JSON`, `TASKMIND_URGENT_WINDOW` (padrão 30 min).
Logs são JSON, uma linha por evento (`app/logging_config.py`).

## Documentação

- [`docs/ARQUITETURA.md`](docs/ARQUITETURA.md): camadas, módulos, fluxo e decisões registradas.
- [`docs/AUDITORIA_MODELS.md`](docs/AUDITORIA_MODELS.md): os 12 achados da auditoria do modelo e suas correções (planilha em `docs/auditoria_models.xlsx`).

## Estrutura

| Camada | Arquivos |
|---|---|
| Domínio (modelos, validação, máquina de estados, exceções) | `app/domain/` |
| Algoritmos / regras | `app/services/{prioritization,dependencies,routing,assignment,simulation}.py` |
| Persistência | `app/repositories/orders.py` (SQLite), `files.py` (JSON) |
| Interface | `app/api.py` (FastAPI), `app/cli.py` |
| Testes, benchmarks, dados | `tests/`, `benchmarks/`, `data/` |
| Documentação e CI | `docs/`, `.github/workflows/tests.yml` |

## Modelo, estados e invariantes (`app/domain/models.py`)

Máquina de estados (qualquer outra transição levanta `InvalidTransitionError`):

| De | Para |
|---|---|
| `pending` | `assigned`, `blocked`, `cancelled` |
| `blocked` | `pending`, `assigned`, `cancelled` (`blocked` é derivado: dependências não entregues) |
| `assigned` | `in_transit`, `pending` (liberação), `cancelled` |
| `in_transit` | `delivered` (não pode ser cancelado) |
| `delivered`, `cancelled` | nenhum (estados finais) |

Invariantes verificadas **na construção** de qualquer `Order` (inclusive via `replace`/`with_status`):

- `assigned`/`in_transit` **exigem** `assigned_to`; `pending`/`blocked`/`cancelled` **não podem** tê-lo; `delivered` guarda o entregador como histórico.
  O banco reforça isso com um `CHECK` na tabela `orders`.
- Um pedido não depende de si mesmo (`SelfDependencyError`); dependências repetidas viram uma só.
- IDs, destinos e nomes de locais: texto não vazio, **sem espaços nas pontas**. `created_at` e `available_at` >= 0.
- JSON com **campos desconhecidos é rejeitado**, com sugestão (`'dependecies' (você quis dizer 'dependencies'?)`).
- `Graph.neighbors()` devolve uma visão somente leitura; uma aresta inválida não cria nós.
- Serviços mudam status **somente** via `with_status`, que valida a transição e as invariantes.

> JSON no formato exato do enunciado com `"status": "assigned"` e sem `assigned_to` é rejeitado com mensagem clara:
> sem saber o entregador não há como contar a carga dele na capacidade.

## Decisões técnicas (e por quê)

**Missão 1 – `rank_orders`.** Uma única chave de ordenação `(não-urgente, -prioridade, tempo restante, created_at, id)`
em `sorted()` (Timsort): O(n log n), nunca compara todos os pares, não altera a lista original. Elegível = `pending`/`blocked`
com todas as dependências *entregues*; `blocked` é tratado como estado derivado e reavaliado. Dependência cancelada ou inexistente
mantém o pedido bloqueado. "Urgente" é inclusivo (≤ 30 min) e inclui atrasados.

**Missão 2 – `validate_dependencies`.** Kahn iterativo, O(V+E): sem recursão (cadeia de 50.000 pedidos não estoura a pilha).
Se sobram nós, um passo O(V) extrai **um ciclo concreto** (`P1 -> P2 -> P3 -> P1`) para a mensagem de erro. Também rejeita IDs
duplicados e inexistentes (listando todos). Autodependência e dependências repetidas são tratadas já na construção do `Order`.

**Missão 3 – `shortest_path`.** Dijkstra com `heapq` e *lazy deletion* (o heap não tem decrease-key). Para assim que o destino
é retirado do heap (medido: destino a 10 saltos expande 10 locais, não 5.000). Pesos negativos são rejeitados **na construção do
grafo**, garantindo a pré-condição do algoritmo. Sem rota → `Route(found=False)`; local inexistente → `UnknownLocationError`.

**Missão 4 – `assign_orders` (heurística gulosa).** Processa pedidos na ordem do ranking; para cada um escolhe o entregador que minimiza
`(perde prazo?, tempo de viagem, id)`. Premissas: cada entregador leva uma carga por rodada (soma dos pesos ≤ capacidade, contando
carga já comprometida); entrega em sequência (posição e horário avançam a cada atribuição). **Limitações:** é gulosa, então pode usar o
entregador mais próximo em um pedido folgado e deixar um pedido seguinte sem opção no prazo; o ótimo global é NP-difícil (VRP com janelas
de tempo). Prazo impossível **não** vira atribuição "mágica": o pedido vai ao entregador mais rápido, marcado `late=True` com motivo `RISCO`.
Sem entregador elegível, o pedido fica sem atribuição com a razão (capacidade / sem rota / ninguém disponível).

**Missão 5 – `OrderRepository.process_order`.** Por que cada peça existe:
`BEGIN IMMEDIATE` (serializa escritores: elimina o *check-then-act*), `UPDATE … WHERE status='pending'` + `rowcount`
(compare-and-set, defesa em profundidade), `assignments.order_id PRIMARY KEY` (o banco recusa dupla atribuição mesmo se a aplicação falhar),
chave de idempotência gravada **na mesma transação** do efeito (sem janela "efeito feito, chave não salva"), e auditoria. Falhas de regra
não consomem a chave. Tentativas rejeitadas são auditadas numa transação separada. Transições inválidas (`delivered → pending`,
`in_transit → cancelled`…) são barradas pela máquina de estados em `domain/models.py`.

**Missão 6 – `simulate`.** Eventos ordenados por `(time, posição)`; `id` repetido é ignorado (idempotência); eventos inválidos são
registrados como `rejected` sem interromper. Após cada evento aplicado, replaneja com `assign_orders` usando entregadores ativos e
**ociosos**. Indisponibilidade libera apenas pedidos `assigned` que ainda não saíram; os `in_transit` continuam. `SimulationResult.fingerprint()`
(SHA-256 do histórico) prova a reprodutibilidade.

## Resultado do estresse (`benchmarks/stress.py`, máquina de desenvolvimento, Python 3.12)

| Cenário | Tempo | Pico de memória | Observação |
|---|---|---|---|
| 10.000 pedidos – ranking | 20 ms | 1,5 MB | O(n log n); teste de escala 4× dados ≈ 4–5× tempo |
| 10.000 pedidos, ~30.000 dependências – validação | 173 ms | 8,6 MB | inclui criar os objetos |
| Ciclo gigante (anel de 10.000) | 17 ms | 3,5 MB | rejeitado, com o caminho do ciclo |
| 200 consultas de rota, mapa de 5.000 locais | 1,4 s (~7 ms cada) | 1,1 MB | early exit confirmado |
| Atribuir 1.000 pedidos × 10 entregadores (mapa 300) | 783 ms | 0,8 MB | 386 atribuídos, 226 em risco |
| Simulação: 2.000 eventos, 200 pedidos | 1,1 s | 3,0 MB | 670 aplicados, 1.331 rejeitados (eventos aleatórios) |
| SQLite: 400 atribuições sequenciais | 765 ms | – | ~520 ops/s (1 commit/fsync cada) |
| SQLite: 400 requisições concorrentes, 100 pedidos | 541 ms | – | **100 atribuições, 0 duplicadas** |

**Gargalos identificados.** (1) Em `assign_orders`, **98% do tempo é Dijkstra** (~3.000 rotas distintas). Como as origens mudam a cada
atribuição, o cache por (origem, destino) ajuda pouco. Melhorias possíveis: Dijkstra completo por origem (uma execução serve todos os destinos),
pré-computar rotas entre locais frequentes, ou A* com heurística geográfica. (2) Replanejar **a cada evento** reordena tudo, mas o ranking é só ~2% do
custo; o custo real também é roteamento. (3) No SQLite, o custo dominante é o commit durável por operação; operações em lote amortizariam isso.
Não fixei metas de velocidade: números acima são para orientar otimização.

## Testes

`tests/` cobre: caminhos normais, entradas vazias, IDs inexistentes, ciclos, mapas desconectados, prazos impossíveis, falta de capacidade (inclusive
o limite exato), repetição de requisições, atribuições concorrentes (8 threads com barreira), falhas injetadas em 3 pontos da transação, corrida
simulada entre leitura e escrita, restrições do próprio banco, invariantes após replanejamento (incluindo 25 fluxos de eventos aleatórios) e
oráculos independentes (comparador par-a-par para a fila, Floyd-Warshall para rotas).

**Auditoria de `models.py` (M-01 a M-12):** 12 achados corrigidos (1 Alto, 5 Médios, 6 Baixos), cada um com teste de regressão, mais um teste de
propriedade (passeios aleatórios sobre a máquina de estados) e 19 mutantes novos, todos detectados.

**Os testes foram verificados com mutação manual:** 30 defeitos injetados de propósito (prioridade invertida, `<` vs `<=`,
`BEGIN` sem `IMMEDIATE`, UPDATE sem condição, idempotência desligada, etc.). Na primeira rodada 2 sobreviveram (limite exato de capacidade e
entregador ocupado recebendo pedido novo); foram adicionados testes e hoje os 30 são detectados.

## Limitações conhecidas

- O `CHECK` de status x `assigned_to` só existe em bancos **criados** por esta versão; um `taskmind.db` antigo precisa ser recriado (ou migrado).

- Atribuição gulosa (ver acima); entregador ocioso só recebe pedidos novos depois de concluir a carga atual (simplificação do replanejamento).
- Cancelar pedido `in_transit` é proibido (regra de negócio assumida; ajuste em `ALLOWED_TRANSITIONS`).
- A simulação é em memória; a API só a alimenta com os dados do banco. `process_order` é a única operação com idempotência persistente.
- SQLite exige arquivo (não `:memory:`), pois cada operação usa uma conexão própria.
- `ORDER_CREATED` revalida o grafo inteiro: O(V+E) por evento.
