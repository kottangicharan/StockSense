"""Pure state-machine rules: no database."""
from decimal import Decimal
from itertools import product

from app.inventory import can_transition, ledger_deltas

STATES = ["draft", "waiting", "ready", "done", "canceled"]
LEGAL = {("draft", "waiting"), ("waiting", "ready"), ("ready", "done"),
         ("draft", "canceled"), ("waiting", "canceled"), ("ready", "canceled")}


def test_only_single_forward_steps_or_cancel_are_legal():
    for frm, to in product(STATES, STATES):
        assert can_transition("delivery", frm, to) == ((frm, to) in LEGAL), (frm, to)
        # Receipts have no stock to wait for: draft -> ready is also allowed.
        assert can_transition("receive", frm, to) == ((frm, to) in LEGAL | {("draft", "ready")}), (frm, to)


def test_ledger_deltas_per_type():
    base = {"source_location_id": 10, "dest_location_id": 20}
    lines = [{"product_id": 1, "qty": Decimal("5")}]
    assert ledger_deltas({**base, "type": "receive", "source_location_id": None}, lines) == [(1, 20, Decimal("5"))]
    assert ledger_deltas({**base, "type": "delivery", "dest_location_id": None}, lines) == [(1, 10, Decimal("-5"))]
    adj = {**base, "type": "adjustment", "dest_location_id": None}  # counted 5
    assert ledger_deltas(adj, lines, on_hand={1: Decimal("8")}) == [(1, 10, Decimal("-3"))]
    assert ledger_deltas(adj, lines, on_hand={1: Decimal("2")}) == [(1, 10, Decimal("3"))]
    assert ledger_deltas(adj, lines, on_hand={1: Decimal("5")}) == []  # count matches: nothing to post
    assert ledger_deltas({**base, "type": "transfer"}, lines) == [(1, 10, Decimal("-5")), (1, 20, Decimal("5"))]


def test_ledger_deltas_multi_line_sorted_for_lock_order():
    op = {"source_location_id": 30, "dest_location_id": 5, "type": "transfer"}
    lines = [{"product_id": 2, "qty": Decimal("1")}, {"product_id": 1, "qty": Decimal("4")}]
    assert [(p, loc) for p, loc, _ in ledger_deltas(op, lines)] == [(1, 5), (1, 30), (2, 5), (2, 30)]
