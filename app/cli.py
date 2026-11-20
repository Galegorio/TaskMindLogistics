"""CLI de demonstração: ``python -m app.cli [--time 100]`` roda o pipeline completo sobre ``data/``."""
from __future__ import annotations

import argparse

from app.config import Settings
from app.logging_config import configure_logging
from app.repositories.files import load_couriers, load_graph, load_orders
from app.services.assignment import assign_orders
from app.services.dependencies import validate_dependencies
from app.services.prioritization import explain_rank, rank_orders


def main() -> None:
    """Valida dependências, ranqueia e atribui os pedidos de ``data/``, explicando cada decisão."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--time", type=float, default=100, help="instante atual da simulação (minutos)")
    args = parser.parse_args()
    settings = Settings.from_env()
    configure_logging("WARNING", settings.log_json)

    orders = load_orders(settings.data_dir / "orders.json")
    couriers = load_couriers(settings.data_dir / "couriers.json")
    graph = load_graph(settings.data_dir / "map.json")

    report = validate_dependencies(orders)
    print("Ordem topológica:", " -> ".join(report.execution_order))
    print("Bloqueados:", {k: list(v) for k, v in report.blocked.items()} or "nenhum")
    print(f"\nFila de prioridade em t={args.time:g}:")
    for pos, o in enumerate(rank_orders(orders, args.time), 1):
        print(f"  {pos}. {explain_rank(o, args.time)}")
    plan = assign_orders(orders, couriers, graph, args.time)
    print("\nAtribuições:")
    for a in plan.assignments:
        print(f"  {a.order_id} -> {a.courier_id} | ETA {a.eta:g} | {a.reason}")
    for u in plan.unassigned:
        print(f"  {u.order_id} sem atribuição: {u.reason}")


if __name__ == "__main__":
    main()
