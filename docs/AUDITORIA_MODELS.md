# Auditoria de `app/domain/models.py`

Auditoria feita sem alterar código, com cada achado reproduzido antes de classificar. Todos foram corrigidos e
cobertos por testes de regressão (`tests/test_models.py`, `test_repository.py`, `test_simulation.py`, `test_assignment.py`).
A planilha `auditoria_models.xlsx` (nesta pasta) traz o mesmo conteúdo com filtros e status.

| ID | Gravidade | Problema | Correção |
|---|---|---|---|
| M-01 | Alto | `assigned`/`in_transit` sem `assigned_to` aceitos; `assign_orders` ignorava o peso (capacidade estourada) | Invariante status × `assigned_to` no modelo e `CHECK` no banco |
| M-02 | Médio | Campos desconhecidos ignorados (`"dependecies"` virava "sem dependências"; `"edge"` gerava mapa vazio) | Rejeição com sugestão de correção; `edges` obrigatório |
| M-03 | Médio | `Graph.neighbors()` expunha o dicionário interno (dava para injetar peso negativo) | Visão somente leitura |
| M-04 | Médio | `blocked → assigned` aceito por simulação e `apply_plan`, recusado por `process_order` | Transição permitida na máquina de estados; todos os serviços usam `with_status` |
| M-05 | Médio | `nodes` do mapa sem validação de tipo | Exige lista de nomes |
| M-06 | Médio | Autodependência só barrada em `validate_dependencies` | Barrada na criação do `Order` |
| M-07 | Baixo | `ensure_transition` com texto estourava `AttributeError` | Coerção para `OrderStatus` |
| M-08 | Baixo | `add_edge` rejeitado deixava nós criados | Valida antes de alterar o estado |
| M-09 | Baixo | `neighbors()` de local inexistente estourava `KeyError` | `UnknownLocationError` |
| M-10 | Baixo | IDs com espaços nas pontas aceitos | Rejeitados |
| M-11 | Baixo | `created_at`/`available_at` negativos aceitos | Exigem valor >= 0 |
| M-12 | Baixo | Remoção de dependências repetidas duplicada em dois módulos | Centralizada no modelo |

## Como a eficácia dos testes foi verificada

Cada correção foi desfeita de propósito (19 variações) e a suíte precisou falhar. As 19 foram detectadas.
