# Arquitetura do TaskMind Logistics

## Camadas e dependências

```
api.py / cli.py            interface (HTTP e terminal)
      │
app/services/              algoritmos e regras de negócio (funções puras, sem I/O)
      │
app/repositories/          persistência: SQLite (orders.py) e arquivos JSON (files.py)
      │
app/domain/                modelos, validação, máquina de estados, exceções
```

Regra de dependência: cada camada só conhece as de baixo. `services` não importa SQLite nem FastAPI; `domain` não
importa nada do projeto (só a biblioteca padrão). O FastAPI é carregado apenas dentro de `create_app`, então o núcleo
roda sem ele.

## Módulos

| Módulo | Responsabilidade | Algoritmo / técnica |
|---|---|---|
| `domain/models.py` | `Order`, `Courier`, `Graph`, estados e transições, invariantes | validação na construção; `with_status` |
| `services/prioritization.py` | fila de pedidos elegíveis | `sorted` com chave composta, O(n log n) |
| `services/dependencies.py` | validação do grafo de dependências, bloqueios | Kahn iterativo, O(V+E) |
| `services/routing.py` | menor caminho | Dijkstra com `heapq`, parada antecipada |
| `services/assignment.py` | distribuição de pedidos | heurística gulosa (limitações no README) |
| `services/simulation.py` | eventos e replanejamento | simulação determinística, `fingerprint` |
| `repositories/orders.py` | persistência, idempotência, auditoria | `BEGIN IMMEDIATE`, update condicional, UNIQUE |
| `config.py` / `logging_config.py` | configuração por ambiente, logs JSON | variáveis `TASKMIND_*` |

## Fluxo de uma atribuição

1. `rank_orders` filtra pedidos elegíveis (status pendente/bloqueado e dependências entregues) e os ordena.
2. `assign_orders` escolhe, pedido a pedido, o entregador que minimiza `(perde prazo?, tempo de viagem, id)`.
3. Na persistência, `process_order` repete as verificações dentro de uma transação e grava efeito, chave de
   idempotência e auditoria de forma atômica.
4. `simulate` aplica eventos e chama `assign_orders` de novo para o que mudou.

## Estados do pedido

`pending → assigned → in_transit → delivered`, com `blocked` (derivado de dependências) e `cancelled`.
A tabela completa de transições e as invariantes estão no README, seção "Modelo, estados e invariantes".

## Decisões registradas

- **Pesos não negativos garantidos pelo tipo**, não pelo algoritmo: `Graph` rejeita pesos negativos e expõe visão somente leitura.
- **Mudança de status por um único caminho** (`with_status`), para que nenhuma camada viole a máquina de estados.
- **Idempotência na mesma transação do efeito**, evitando a janela "efeito aplicado, chave não salva".
- **Falhas de regra não consomem a chave de idempotência**; o cliente pode corrigir e reenviar.
- **Heurística gulosa assumida como limitação**, não como defeito: o ótimo global é NP-difícil.
