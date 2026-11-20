"""Validação de entrada e mensagens de erro claras."""
import json
import random

import pytest

from app.domain.exceptions import (InvalidTransitionError, NegativeWeightError, SelfDependencyError,
                                   UnknownLocationError, ValidationError)
from app.domain.models import (ALLOWED_TRANSITIONS, COMMITTED_STATUSES, Courier, Graph, Order, OrderStatus,
                               ensure_transition, with_status)
from app.repositories.files import load_couriers, load_graph, load_orders
from tests.factories import make_order

VALID = {"id": "P1", "title": "t", "priority": 3, "created_at": 0, "deadline": 10, "weight": 2, "destination": "B"}


def test_from_dict_ok_with_defaults():
    o = Order.from_dict(VALID)
    assert o.status is OrderStatus.PENDING and o.dependencies == ()


@pytest.mark.parametrize("priority", [0, 6, -1, 3.5, "3", True, None])
def test_invalid_priority(priority):
    with pytest.raises(ValidationError, match="priority"):
        Order.from_dict({**VALID, "priority": priority})


@pytest.mark.parametrize("weight", [0, -5, "x", None, float("nan"), float("inf"), True])
def test_invalid_weight(weight):
    with pytest.raises(ValidationError, match="weight"):
        Order.from_dict({**VALID, "weight": weight})


def test_deadline_before_creation_rejected():
    with pytest.raises(ValidationError, match="deadline"):
        Order.from_dict({**VALID, "created_at": 50, "deadline": 10})


def test_missing_fields_are_listed():
    with pytest.raises(ValidationError, match="weight, destination"):
        Order.from_dict({k: v for k, v in VALID.items() if k not in ("weight", "destination")})


def test_invalid_status_dependencies_and_type():
    with pytest.raises(ValidationError, match="status"):
        Order.from_dict({**VALID, "status": "flying"})
    with pytest.raises(ValidationError, match="dependencies"):
        Order.from_dict({**VALID, "dependencies": "P2"})
    with pytest.raises(ValidationError, match="dependencies"):
        Order.from_dict({**VALID, "dependencies": [1, 2]})
    with pytest.raises(ValidationError):
        Order.from_dict("não é objeto")


def test_roundtrip_to_dict():
    o = make_order("P7", dependencies=("P1", "P2"))
    assert Order.from_dict(o.to_dict()) == o


def test_courier_validation():
    assert Courier.from_dict({"id": "D", "location": "A", "capacity": 5}).active is True
    with pytest.raises(ValidationError, match="capacity"):
        Courier.from_dict({"id": "D", "location": "A", "capacity": 0})
    with pytest.raises(ValidationError, match="active"):
        Courier.from_dict({"id": "D", "location": "A", "capacity": 5, "active": "yes"})
    with pytest.raises(ValidationError, match="location"):
        Courier.from_dict({"id": "D", "capacity": 5})


def test_graph_rejects_bad_edges():
    with pytest.raises(NegativeWeightError, match="negativo"):
        Graph.from_dict({"edges": [{"from": "A", "to": "B", "time": -1}]})
    with pytest.raises(ValidationError):
        Graph.from_dict({"edges": [{"from": "A", "to": "B"}]})
    with pytest.raises(ValidationError):
        Graph.from_dict({"edges": [{"from": "A", "to": "B", "time": float("inf")}]})


def test_state_machine():
    ensure_transition("P1", OrderStatus.PENDING, OrderStatus.ASSIGNED)
    for current, new in [(OrderStatus.DELIVERED, OrderStatus.PENDING), (OrderStatus.CANCELLED, OrderStatus.ASSIGNED),
                         (OrderStatus.IN_TRANSIT, OrderStatus.CANCELLED), (OrderStatus.PENDING, OrderStatus.DELIVERED),
                         (OrderStatus.ASSIGNED, OrderStatus.DELIVERED)]:
        with pytest.raises(InvalidTransitionError, match="não permitida"):
            ensure_transition("P1", current, new)


def test_loaders_report_clear_errors(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{nao é json", encoding="utf-8")
    with pytest.raises(ValidationError, match="JSON inválido"):
        load_orders(bad)
    latin = tmp_path / "latin1.json"
    latin.write_bytes("[]  é".encode("cp1252"))  # arquivo salvo fora de UTF-8 (comum no Windows)
    with pytest.raises(ValidationError, match="UTF-8"):
        load_orders(latin)
    with pytest.raises(ValidationError, match="não encontrado"):
        load_couriers(tmp_path / "nope.json")
    obj = tmp_path / "obj.json"
    obj.write_text(json.dumps({"a": 1}), encoding="utf-8")
    with pytest.raises(ValidationError, match="lista"):
        load_orders(obj)


def test_bundled_data_files_are_valid():
    from pathlib import Path
    data = Path(__file__).resolve().parent.parent / "data"
    assert len(load_orders(data / "orders.json")) >= 5
    assert len(load_couriers(data / "couriers.json")) >= 2
    assert "A" in load_graph(data / "map.json")


# =====================================================================================================
# Regressões da auditoria de models.py (M-01 a M-12)
# =====================================================================================================
S = OrderStatus


def invariant_ok(status, assigned_to):
    """Oráculo independente da regra status x assigned_to."""
    if status in (S.ASSIGNED, S.IN_TRANSIT):
        return assigned_to is not None
    if status in (S.PENDING, S.BLOCKED, S.CANCELLED):
        return assigned_to is None
    return True  # delivered: o entregador é só histórico


def test_m01_status_and_assigned_to_invariant_for_every_combination():
    for status in S:
        for courier in (None, "D1"):
            if invariant_ok(status, courier):
                assert make_order("P1", status=status, assigned_to=courier).status is status
            else:
                with pytest.raises(ValidationError, match="assigned_to"):
                    make_order("P1", status=status, assigned_to=courier)


def test_m01_json_with_assigned_status_but_no_courier_is_rejected_with_clear_message():
    with pytest.raises(ValidationError, match="exige 'assigned_to'"):
        Order.from_dict({**VALID, "status": "assigned"})
    assert Order.from_dict({**VALID, "status": "assigned", "assigned_to": "D1"}).assigned_to == "D1"


def test_m02_unknown_fields_are_rejected_with_suggestion():
    with pytest.raises(ValidationError, match=r"'dependecies' \(você quis dizer 'dependencies'\?\)"):
        Order.from_dict({**VALID, "dependecies": ["P0"]})
    with pytest.raises(ValidationError, match="'statu'"):
        Order.from_dict({**VALID, "statu": "cancelled"})
    with pytest.raises(ValidationError, match="'weigth'"):  # typo em campo obrigatório também é apontado
        Order.from_dict({k: v for k, v in VALID.items() if k != "weight"} | {"weigth": 2})
    with pytest.raises(ValidationError, match="'capacidade'|'capacity'"):
        Courier.from_dict({"id": "D", "location": "A", "capacty": 5})
    with pytest.raises(ValidationError, match="'edge' \\(você quis dizer 'edges'"):
        Graph.from_dict({"edge": [{"from": "A", "to": "B", "time": 1}]})
    with pytest.raises(ValidationError, match="'tiem'"):
        Graph.from_dict({"edges": [{"from": "A", "to": "B", "tiem": 1}]})
    with pytest.raises(ValidationError, match="'edges' ausente"):
        Graph.from_dict({})


def test_m03_neighbors_is_read_only_so_weights_cannot_be_corrupted():
    g = make_graph_ab()
    with pytest.raises(TypeError):
        g.neighbors("A")["B"] = -100
    with pytest.raises(TypeError):
        del g.neighbors("A")["B"]
    assert g.neighbors("A")["B"] == 5


def make_graph_ab():
    g = Graph()
    g.add_edge("A", "B", 5)
    return g


def test_m04_blocked_can_be_assigned_and_is_consistent_everywhere():
    assert OrderStatus.ASSIGNED in ALLOWED_TRANSITIONS[OrderStatus.BLOCKED]
    ensure_transition("P1", OrderStatus.BLOCKED, OrderStatus.ASSIGNED)
    o = with_status(make_order("PB", status=S.BLOCKED), S.ASSIGNED, "D1")
    assert (o.status, o.assigned_to) == (S.ASSIGNED, "D1")


@pytest.mark.parametrize("nodes", [5, "AB", {"A": 1}, None])
def test_m05_nodes_must_be_a_list(nodes):
    with pytest.raises(ValidationError, match="nodes"):
        Graph.from_dict({"edges": [], "nodes": nodes})
    with pytest.raises(ValidationError):
        Graph.from_dict({"edges": [], "nodes": ["A", 7]})


def test_m06_self_dependency_is_rejected_at_construction():
    with pytest.raises(SelfDependencyError, match="'P1'"):
        Order.from_dict({**VALID, "dependencies": ["P1"]})


def test_m07_ensure_transition_accepts_plain_strings_and_never_crashes():
    ensure_transition("P1", "pending", "assigned")
    with pytest.raises(InvalidTransitionError, match="delivered -> pending"):
        ensure_transition("P1", "delivered", OrderStatus.PENDING)
    with pytest.raises(ValidationError, match="status inválido"):
        ensure_transition("P1", "pending", "flying")
    with pytest.raises(ValidationError, match="status inválido"):
        ensure_transition("P1", None, OrderStatus.PENDING)


@pytest.mark.parametrize("a,b,t", [("X", "Y", -1), ("X", "Y", float("nan")), ("X", "Y", True), ("X", "Y", "5"),
                                   ("X", "", 1), ("X", " Y", 1), ("X", None, 1)])
def test_m08_rejected_edge_leaves_graph_untouched(a, b, t):
    g = Graph()
    g.add_edge("A", "B", 1)
    before = (g.nodes(), dict(g.neighbors("A")))
    with pytest.raises(ValidationError):
        g.add_edge(a, b, t)
    assert (g.nodes(), dict(g.neighbors("A"))) == before


def test_m09_neighbors_of_unknown_location_raises_domain_error():
    with pytest.raises(UnknownLocationError, match="'Q'"):
        Graph().neighbors("Q")


@pytest.mark.parametrize("bad", [" P1", "P1 ", " ", "\tP1", "P1\n"])
def test_m10_text_ids_with_edge_whitespace_are_rejected(bad):
    with pytest.raises(ValidationError):
        make_order(bad)
    with pytest.raises(ValidationError):
        make_order("P1", destination=bad)
    with pytest.raises(ValidationError):
        make_order("P1", dependencies=(bad,))
    with pytest.raises(ValidationError):
        Courier(id=bad, location="A", capacity=1)
    with pytest.raises(ValidationError):
        Courier(id="D1", location=bad, capacity=1)
    with pytest.raises(ValidationError):
        Graph().add_node(bad)


def test_m10_inner_spaces_are_fine():
    assert make_order("Pedido 1", destination="Centro Sul").id == "Pedido 1"


def test_m11_negative_times_are_rejected_and_zero_is_valid():
    with pytest.raises(ValidationError, match="created_at"):
        make_order("P1", created_at=-1, deadline=5)
    with pytest.raises(ValidationError, match="available_at"):
        Courier(id="D1", location="A", capacity=1, available_at=-0.5)
    assert make_order("P1", created_at=0, deadline=0).deadline == 0
    assert Courier(id="D1", location="A", capacity=1, available_at=0).available_at == 0


def test_m12_repeated_dependencies_are_collapsed_in_one_place_keeping_order():
    o = make_order("P9", dependencies=("B", "A", "B", "A", "C"))
    assert o.dependencies == ("B", "A", "C")
    assert Order.from_dict(o.to_dict()) == o


def test_with_status_validates_transition_and_manages_courier():
    pending = make_order("P1")
    assigned = with_status(pending, S.ASSIGNED, "D1")
    assert with_status(assigned, S.IN_TRANSIT).assigned_to == "D1"          # mantém
    assert with_status(assigned, S.PENDING).assigned_to is None             # libera
    assert with_status(assigned, S.CANCELLED).assigned_to is None
    delivered = with_status(with_status(assigned, S.IN_TRANSIT), S.DELIVERED)
    assert delivered.assigned_to == "D1"                                    # histórico
    with pytest.raises(InvalidTransitionError):
        with_status(pending, S.DELIVERED)
    with pytest.raises(ValidationError, match="assigned_to"):
        with_status(pending, S.ASSIGNED)                                    # atribuir exige entregador
    assert pending.status is S.PENDING                                      # original intocado


def test_random_walks_over_the_state_machine_respect_transitions_and_invariants():
    rng = random.Random(11)
    for _ in range(300):
        order = make_order("P1")
        for _step in range(15):
            target = rng.choice(list(S))
            if target in ALLOWED_TRANSITIONS[order.status]:
                order = with_status(order, target, "D1") if target is S.ASSIGNED else with_status(order, target)
                assert order.status is target and invariant_ok(order.status, order.assigned_to)
                if order.status in COMMITTED_STATUSES:
                    assert order.assigned_to == "D1"
            else:
                before = order
                with pytest.raises(InvalidTransitionError):
                    with_status(order, target, "D1") if target is S.ASSIGNED else with_status(order, target)
                assert order == before
        # estados finais nunca voltam atrás
    for final in (S.DELIVERED, S.CANCELLED):
        assert ALLOWED_TRANSITIONS[final] == frozenset()
