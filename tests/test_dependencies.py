"""Missão 2."""
import pytest

from app.domain.exceptions import (CycleError, DuplicateOrderError, SelfDependencyError, UnknownDependencyError)
from app.domain.models import OrderStatus
from app.services.dependencies import find_blocked, validate_dependencies
from tests.factories import make_order


def test_empty():
    r = validate_dependencies([])
    assert r.execution_order == () and dict(r.blocked) == {}


def test_valid_order_respects_dependencies_diamond():
    orders = [make_order("D", dependencies=("B", "C")), make_order("B", dependencies=("A",)),
              make_order("C", dependencies=("A",)), make_order("A")]
    order = validate_dependencies(orders).execution_order
    pos = {oid: i for i, oid in enumerate(order)}
    assert sorted(order) == ["A", "B", "C", "D"]
    assert pos["A"] < pos["B"] < pos["D"] and pos["A"] < pos["C"] < pos["D"]


def test_unknown_dependency_lists_all_missing():
    orders = [make_order("P1", dependencies=("X",)), make_order("P2", dependencies=("Y", "P1"))]
    with pytest.raises(UnknownDependencyError) as exc:
        validate_dependencies(orders)
    assert exc.value.missing == (("P1", "X"), ("P2", "Y"))
    assert "'X'" in str(exc.value)


def test_self_dependency_is_rejected_when_the_order_is_built():
    with pytest.raises(SelfDependencyError, match="P1"):
        make_order("P1", dependencies=("P1",))
    with pytest.raises(SelfDependencyError):
        make_order("P1", dependencies=("P2", "P1"))


def test_cycle_reports_actual_path():
    orders = [make_order("P1", dependencies=("P2",)), make_order("P2", dependencies=("P3",)),
              make_order("P3", dependencies=("P1",))]
    with pytest.raises(CycleError) as exc:
        validate_dependencies(orders)
    cycle = exc.value.cycle
    assert cycle[0] == cycle[-1] and set(cycle) == {"P1", "P2", "P3"} and len(cycle) == 4
    deps = {o.id: o.dependencies for o in orders}
    assert all(b in deps[a] for a, b in zip(cycle, cycle[1:]))  # cada passo é uma dependência real
    assert "->" in str(exc.value)


def test_cycle_found_inside_larger_valid_graph():
    orders = [make_order("OK1"), make_order("OK2", dependencies=("OK1",)),
              make_order("A", dependencies=("OK2", "B")), make_order("B", dependencies=("C",)),
              make_order("C", dependencies=("B",)), make_order("TAIL", dependencies=("A",))]
    with pytest.raises(CycleError) as exc:
        validate_dependencies(orders)
    assert set(exc.value.cycle) == {"B", "C"}  # não acusa nós inocentes (A, TAIL, OK*)


def test_two_disjoint_cycles_still_rejected():
    orders = [make_order("A", dependencies=("B",)), make_order("B", dependencies=("A",)),
              make_order("C", dependencies=("D",)), make_order("D", dependencies=("C",))]
    with pytest.raises(CycleError):
        validate_dependencies(orders)


def test_duplicate_ids_and_repeated_dependencies():
    with pytest.raises(DuplicateOrderError):
        validate_dependencies([make_order("P1"), make_order("P1")])
    r = validate_dependencies([make_order("P1"), make_order("P2", dependencies=("P1", "P1"))])
    assert r.execution_order == ("P1", "P2")


def test_blocked_report():
    orders = [make_order("P1"), make_order("P2", dependencies=("P1",)),
              make_order("P3", status=OrderStatus.DELIVERED), make_order("P4", dependencies=("P3",)),
              make_order("P5", status=OrderStatus.CANCELLED), make_order("P6", dependencies=("P5",)),
              make_order("P7", dependencies=("P1", "P3"), status=OrderStatus.DELIVERED)]
    blocked = find_blocked(orders)
    assert blocked == {"P2": ("P1",), "P6": ("P5",)}  # P4 liberado; P7 já entregue não conta
    assert dict(validate_dependencies(orders).blocked) == blocked


def test_very_deep_chain_has_no_recursion_limit_and_is_linear():
    n = 50_000
    orders = [make_order("P0")] + [make_order(f"P{i}", dependencies=(f"P{i-1}",)) for i in range(1, n)]
    assert validate_dependencies(orders).execution_order == tuple(f"P{i}" for i in range(n))
