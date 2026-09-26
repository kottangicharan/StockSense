"""Operations engine: state machine, ledger + quant writes under row locks, idempotency, and routes.

Services are plain functions (no HTTP); routes call them and broadcast only after they return (i.e. after commit).
"""
from collections import defaultdict
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, Query
from psycopg.types.json import Jsonb

from . import db
from .auth import current_user, require_role
from .db import DomainError
from .schemas import (
    DashboardOut, LedgerOut, LineIn, LocationIn, LocationOut, MoveOut, OpCounts, OperationDetail, OperationIn,
    OperationOut, OpStatus, OpType, ProductIn, ProductOut, ProductPatch, QuantOut, StockAlert, TransitionIn,
    WarehouseIn, WarehouseOut,
)
from .ws import publish

# --- pure logic (unit-tested without a DB) ---

NEXT: dict[str, str] = {"draft": "waiting", "waiting": "ready", "ready": "done"}


def can_transition(op_type: str, frm: str, to: str) -> bool:
    """Forward-only, one step at a time; receipts may skip 'waiting'; any open operation can be canceled."""
    return (NEXT.get(frm) == to or (to == "canceled" and frm in NEXT)
            or (op_type == "receive" and (frm, to) == ("draft", "ready")))


def ledger_deltas(op: dict, lines: list[dict], on_hand: dict[int, Decimal] | None = None) -> list[tuple[int, int, Decimal]]:
    """(product_id, location_id, delta) rows an operation posts when it reaches 'done'.

    An adjustment line's qty is the counted quantity, so its delta is counted - on_hand (on_hand read under lock).
    Zero deltas are dropped. Sorted so every transaction locks quant rows in the same order: no deadlocks.
    """
    src, dst = op["source_location_id"], op["dest_location_id"]
    rows = []
    for line in lines:
        product_id, qty = line["product_id"], line["qty"]
        if op["type"] == "adjustment":
            moves = [(src, qty - on_hand[product_id])]
        else:
            moves = {
                "receive": [(dst, qty)],
                "transfer": [(src, -qty), (dst, qty)],
                "delivery": [(src, -qty)],
            }[op["type"]]
        rows += [(product_id, loc, delta) for loc, delta in moves if delta != 0]
    return sorted(rows)


def _require_manager_for_adjustment(op_type: str, actor: dict) -> None:
    if op_type == "adjustment" and actor["role"] != "manager":
        raise DomainError(403, "forbidden", "Only managers can create or move stock adjustments")


# --- ledger / quants (always called inside the caller's transaction) ---

def lock_quant(cur, product_id: int, location_id: int) -> Decimal:
    """Lock the quant row (creating it at 0) and return its qty. Concurrent writers of a SKU/location serialize here."""
    cur.execute(
        "INSERT INTO quants (product_id, location_id, qty) VALUES (%s, %s, 0) ON CONFLICT DO NOTHING",
        (product_id, location_id),
    )
    return cur.execute(
        "SELECT qty FROM quants WHERE product_id = %s AND location_id = %s FOR UPDATE",
        (product_id, location_id),
    ).fetchone()["qty"]


def recompute_quant(cur, product_id: int, location_id: int, delta: Decimal) -> None:
    qty = lock_quant(cur, product_id, location_id)
    new_qty = qty + delta
    if new_qty < 0:
        raise DomainError(409, "insufficient_stock", f"Only {qty} of product {product_id} on hand at location {location_id}")
    cur.execute(
        "UPDATE quants SET qty = %s WHERE product_id = %s AND location_id = %s",
        (new_qty, product_id, location_id),
    )


def append_ledger_entry(cur, operation_id: int, product_id: int, location_id: int, delta: Decimal, actor_id: int) -> None:
    # Insert-only by design; a DB trigger rejects UPDATE/DELETE/TRUNCATE on this table.
    cur.execute(
        "INSERT INTO ledger (operation_id, product_id, location_id, delta, actor_id) VALUES (%s, %s, %s, %s, %s)",
        (operation_id, product_id, location_id, delta, actor_id),
    )


def post_to_ledger(cur, op: dict, actor_id: int) -> None:
    """Apply a 'done' operation: ledger rows + quants, in the caller's transaction."""
    lines = cur.execute(
        "SELECT product_id, qty FROM operation_lines WHERE operation_id = %s ORDER BY product_id", (op["id"],)
    ).fetchall()
    on_hand = None
    if op["type"] == "adjustment":  # product order = the same lock order ledger_deltas sorts into
        on_hand = {line["product_id"]: lock_quant(cur, line["product_id"], op["source_location_id"]) for line in lines}
    for product_id, location_id, delta in ledger_deltas(op, lines, on_hand):
        recompute_quant(cur, product_id, location_id, delta)  # raises -> whole transaction rolls back
        append_ledger_entry(cur, op["id"], product_id, location_id, delta, actor_id)


# --- idempotency ---

def claim_idempotency_key(cur, user_id: int, key: str | None, fingerprint: str) -> dict | None:
    """Returns the stored response if this key already completed; None if we own it now.

    A concurrent request with the same key blocks on the PK until the first transaction commits,
    then replays its response — so a double-click can never run the transition twice.
    """
    if key is None:
        return None
    claimed = cur.execute(
        "INSERT INTO idempotency_keys (user_id, key, fingerprint) VALUES (%s, %s, %s) "
        "ON CONFLICT DO NOTHING RETURNING key",
        (user_id, key, fingerprint),
    ).fetchone()
    if claimed:
        return None
    prev = cur.execute(
        "SELECT fingerprint, response FROM idempotency_keys WHERE user_id = %s AND key = %s", (user_id, key)
    ).fetchone()
    if prev["fingerprint"] != fingerprint:
        raise DomainError(422, "idempotency_key_reused", "Idempotency-Key was already used for a different request")
    return prev["response"]


def store_idempotent_response(cur, user_id: int, key: str | None, response: dict) -> None:
    if key is not None:
        cur.execute(
            "UPDATE idempotency_keys SET response = %s WHERE user_id = %s AND key = %s",
            (Jsonb(response), user_id, key),
        )


# --- operation services ---

OP_SELECT = "SELECT o.*, u.login_id AS responsible FROM operations o JOIN users u ON u.id = o.created_by"


def _has_line(cond: str) -> str:
    """SQL: operation o has a line whose product p matches cond."""
    return ("EXISTS (SELECT 1 FROM operation_lines l JOIN products p ON p.id = l.product_id "
            f"WHERE l.operation_id = o.id AND {cond})")


def _load(cur, clause: str = "", params: list | tuple = (), tail: str = "") -> list[OperationOut]:
    """Operations matching clause, each with its lines and responsible user."""
    ops = cur.execute(f"{OP_SELECT} {clause} {tail}", params).fetchall()
    lines = defaultdict(list)
    for line in cur.execute(
        "SELECT l.operation_id, l.product_id, p.sku, p.name AS product_name, p.uom, l.qty "
        "FROM operation_lines l JOIN products p ON p.id = l.product_id WHERE l.operation_id = ANY(%s) ORDER BY l.id",
        ([o["id"] for o in ops],),
    ).fetchall():
        lines[line["operation_id"]].append(line)
    return [OperationOut.model_validate({**o, "lines": lines[o["id"]]}) for o in ops]


def _load_one(cur, op_id: int) -> OperationOut:
    found = _load(cur, "WHERE o.id = %s", (op_id,))
    if not found:
        raise DomainError(404, "not_found", f"Operation {op_id} not found")
    return found[0]


def _insert_operation(cur, data: OperationIn, actor: dict, status: str = "draft") -> dict:
    home = data.dest_location_id if data.type == "receive" else data.source_location_id
    # Locking the warehouse row serializes reference numbering (count + 1) within that warehouse.
    loc = cur.execute(
        "SELECT l.warehouse_id FROM locations l JOIN warehouses w ON w.id = l.warehouse_id WHERE l.id = %s FOR UPDATE OF w",
        (home,),
    ).fetchone()
    if not loc:
        raise DomainError(422, "unknown_location", f"Location {home} does not exist")
    row = cur.execute(
        "INSERT INTO operations (reference, type, status, source_location_id, dest_location_id, warehouse_id, "
        "scheduled_date, partner, delivery_address, note, created_by) VALUES ("
        "op_reference((SELECT short_code FROM warehouses WHERE id = %(wh)s), %(type)s, "
        "(SELECT count(*) + 1 FROM operations WHERE warehouse_id = %(wh)s AND type = %(type)s)), "
        "%(type)s, %(status)s, %(src)s, %(dst)s, %(wh)s, COALESCE(%(date)s, current_date), %(partner)s, %(addr)s, "
        "%(note)s, %(actor)s) RETURNING *",
        {"wh": loc["warehouse_id"], "type": data.type, "status": status, "src": data.source_location_id,
         "dst": data.dest_location_id, "date": data.scheduled_date, "partner": data.partner,
         "addr": data.delivery_address, "note": data.note, "actor": actor["id"]},
    ).fetchone()
    cur.executemany(
        "INSERT INTO operation_lines (operation_id, product_id, qty) VALUES (%s, %s, %s)",
        [(row["id"], line.product_id, line.qty) for line in data.lines],
    )
    cur.execute(
        "INSERT INTO operation_transitions (operation_id, from_status, to_status, actor_id) VALUES (%s, NULL, %s, %s)",
        (row["id"], status, actor["id"]),
    )
    return row


def create_operation(data: OperationIn, actor: dict, idempotency_key: str | None = None) -> tuple[OperationOut, bool]:
    """Returns (operation, created). created=False means an idempotent replay."""
    _require_manager_for_adjustment(data.type, actor)
    fingerprint = "create:" + data.model_dump_json()
    with db.tx() as cur:
        replay = claim_idempotency_key(cur, actor["id"], idempotency_key, fingerprint)
        if replay is not None:
            return OperationOut.model_validate(replay), False
        op = _load_one(cur, _insert_operation(cur, data, actor)["id"])
        store_idempotent_response(cur, actor["id"], idempotency_key, op.model_dump(mode="json"))
    return op, True


def transition_operation(
    op_id: int, frm: str, to: str, actor: dict, idempotency_key: str | None
) -> tuple[OperationOut, bool]:
    """Move an operation one step forward, or cancel it. On 'done', post ledger rows and update quants — all in ONE transaction.

    Returns (operation, changed). changed=False means an idempotent replay.
    """
    with db.tx() as cur:
        replay = claim_idempotency_key(cur, actor["id"], idempotency_key, f"transition:{op_id}:{frm}:{to}")
        if replay is not None:
            return OperationOut.model_validate(replay), False
        # Row lock on the operation: two validates of the SAME operation serialize, the second sees 'done'.
        op = cur.execute("SELECT * FROM operations WHERE id = %s FOR UPDATE", (op_id,)).fetchone()
        if not op:
            raise DomainError(404, "not_found", f"Operation {op_id} not found")
        _require_manager_for_adjustment(op["type"], actor)
        if op["status"] != frm:
            raise DomainError(409, "stale_state", f"Operation is '{op['status']}', not '{frm}'")
        if not can_transition(op["type"], frm, to):
            raise DomainError(409, "illegal_transition", f"Cannot move {frm} -> {to}; allowed: {frm} -> {NEXT.get(frm)}")
        if to == "done":
            post_to_ledger(cur, op, actor["id"])
        cur.execute("UPDATE operations SET status = %s WHERE id = %s", (to, op_id))
        cur.execute(
            "INSERT INTO operation_transitions (operation_id, from_status, to_status, actor_id) VALUES (%s, %s, %s, %s)",
            (op_id, frm, to, actor["id"]),
        )
        result = _load_one(cur, op_id)
        store_idempotent_response(cur, actor["id"], idempotency_key, result.model_dump(mode="json"))
    return result, True


def _search(q: str | None):
    """SKU / name search condition for db.where (products aliased p)."""
    return q and ("(p.sku ILIKE %s OR p.name ILIKE %s)", [f"%{q}%"] * 2)


def _at_location(location_id: int | None):
    return location_id is not None and ("%s IN (o.source_location_id, o.dest_location_id)", [location_id])


def list_operations(
    type: str | None = None, status: str | None = None, warehouse_id: int | None = None,
    category: str | None = None, location_id: int | None = None, q: str | None = None, limit: int = 200,
) -> list[OperationOut]:
    """q matches reference, partner (contact), or any line's SKU / product name."""
    clause, params = db.where(
        {"o.type": type, "o.status": status, "o.warehouse_id": warehouse_id},
        _at_location(location_id),
        category is not None and (_has_line("p.category = %s"), [category]),
        q and (f"(o.reference ILIKE %s OR o.partner ILIKE %s OR {_has_line('(p.sku ILIKE %s OR p.name ILIKE %s)')})",
               [f"%{q}%"] * 4),
    )
    with db.tx() as cur:
        return _load(cur, clause, [*params, limit], "ORDER BY o.scheduled_date, o.id LIMIT %s")


def get_operation(op_id: int) -> OperationDetail:
    with db.tx() as cur:
        op = _load_one(cur, op_id)
        transitions = cur.execute(
            "SELECT from_status, to_status, actor_id, at FROM operation_transitions WHERE operation_id = %s ORDER BY id",
            (op_id,),
        ).fetchall()
        ledger = cur.execute("SELECT * FROM ledger WHERE operation_id = %s ORDER BY id", (op_id,)).fetchall()
    return OperationDetail.model_validate({**op.model_dump(), "transitions": transitions, "ledger": ledger})


def list_moves(
    type: str | None = None, status: str | None = None, warehouse_id: int | None = None, location_id: int | None = None,
    product_id: int | None = None, q: str | None = None, limit: int = 500,
) -> list[MoveOut]:
    """Move History: one row per operation line. q matches reference, partner, SKU or product name."""
    clause, params = db.where(
        {"o.type": type, "o.status": status, "o.warehouse_id": warehouse_id, "l.product_id": product_id},
        _at_location(location_id),
        q and ("(o.reference ILIKE %s OR o.partner ILIKE %s OR p.sku ILIKE %s OR p.name ILIKE %s)", [f"%{q}%"] * 4),
    )
    with db.tx() as cur:
        rows = cur.execute(
            "SELECT o.id AS operation_id, o.reference, o.type, o.status, o.scheduled_date, o.partner, l.product_id, "
            "p.sku, p.name AS product_name, p.uom, l.qty, "
            "CASE WHEN o.type = 'receive' THEN o.partner ELSE src.name END AS from_location, "
            "CASE WHEN o.type = 'delivery' THEN o.partner ELSE dst.name END AS to_location "
            "FROM operation_lines l JOIN operations o ON o.id = l.operation_id JOIN products p ON p.id = l.product_id "
            "LEFT JOIN locations src ON src.id = o.source_location_id "
            "LEFT JOIN locations dst ON dst.id = o.dest_location_id "
            f"{clause} ORDER BY o.scheduled_date DESC, o.id DESC, l.id LIMIT %s",
            (*params, limit),
        ).fetchall()
    return [MoveOut.model_validate(r) for r in rows]


def list_quants(warehouse_id: int | None = None, product_id: int | None = None, category: str | None = None,
                location_id: int | None = None, q: str | None = None) -> list[QuantOut]:
    clause, params = db.where(
        {"l.warehouse_id": warehouse_id, "q.product_id": product_id, "p.category": category, "q.location_id": location_id},
        _search(q),
    )
    with db.tx() as cur:
        rows = cur.execute(
            "SELECT q.product_id, p.sku, p.name AS product_name, p.category, p.uom, p.unit_cost, q.location_id, "
            "l.name AS location_name, l.warehouse_id, q.qty, q.qty - COALESCE(("
            "SELECT SUM(ol.qty) FROM operation_lines ol JOIN operations o ON o.id = ol.operation_id "
            "WHERE ol.product_id = q.product_id AND o.source_location_id = q.location_id "
            "AND o.type IN ('delivery', 'transfer') AND o.status NOT IN ('done', 'canceled')), 0) AS free_qty "
            "FROM quants q JOIN products p ON p.id = q.product_id JOIN locations l ON l.id = q.location_id "
            f"{clause} ORDER BY p.sku, l.name",
            params,
        ).fetchall()
    return [QuantOut.model_validate(r) for r in rows]


def create_product(data: ProductIn, actor: dict) -> dict:
    """Insert a product; optional initial stock is posted as a done adjustment so quants still equal SUM(ledger)."""
    with db.tx() as cur:
        product = cur.execute(
            "INSERT INTO products (sku, name, category, uom, min_qty, unit_cost) VALUES (%s, %s, %s, %s, %s, %s) "
            "RETURNING *",
            (data.sku, data.name, data.category, data.uom, data.min_qty, data.unit_cost),
        ).fetchone()
        if data.initial_qty > 0:
            count = OperationIn(type="adjustment", lines=[LineIn(product_id=product["id"], qty=data.initial_qty)],
                                source_location_id=data.initial_location_id, note="Initial stock")
            post_to_ledger(cur, _insert_operation(cur, count, actor, status="done"), actor["id"])
    return product


def update_product(product_id: int, data: ProductPatch) -> dict:
    fields = data.model_dump(exclude_unset=True)  # keys are ProductPatch field names, never user input
    with db.tx() as cur:
        if fields:
            sets = ", ".join(f"{col} = %s" for col in fields)
            row = cur.execute(f"UPDATE products SET {sets} WHERE id = %s RETURNING *", (*fields.values(), product_id)).fetchone()
        else:
            row = cur.execute("SELECT * FROM products WHERE id = %s", (product_id,)).fetchone()
    if not row:
        raise DomainError(404, "not_found", f"Product {product_id} not found")
    return row


def dashboard(warehouse_id: int | None = None, category: str | None = None) -> DashboardOut:
    # ponytail: aggregates every product in one pass; fine to tens of thousands of SKUs, paginate alerts beyond that.
    with db.tx() as cur:
        stock = cur.execute(
            "SELECT p.id AS product_id, p.sku, p.name, p.uom, p.min_qty, "
            "COALESCE(SUM(q.qty) FILTER (WHERE %(wh)s::bigint IS NULL OR l.warehouse_id = %(wh)s), 0) AS on_hand "
            "FROM products p LEFT JOIN quants q ON q.product_id = p.id LEFT JOIN locations l ON l.id = q.location_id "
            "WHERE %(cat)s::text IS NULL OR p.category = %(cat)s GROUP BY p.id ORDER BY p.sku",
            {"wh": warehouse_id, "cat": category},
        ).fetchall()
        clause, params = db.where({"o.warehouse_id": warehouse_id},
                                  category is not None and (_has_line("p.category = %s"), [category]))
        counts = {r["type"]: OpCounts.model_validate(r) for r in cur.execute(
            "SELECT o.type, "
            "count(*) FILTER (WHERE o.status NOT IN ('done', 'canceled')) AS open, "
            "count(*) FILTER (WHERE o.status = 'ready') AS ready, "
            "count(*) FILTER (WHERE o.status = 'waiting') AS waiting, "
            "count(*) FILTER (WHERE o.status NOT IN ('done', 'canceled') AND o.scheduled_date < current_date) AS late, "
            "count(*) FILTER (WHERE o.status NOT IN ('done', 'canceled') AND o.scheduled_date > current_date) AS upcoming "
            f"FROM operations o {clause} GROUP BY o.type",
            params,
        ).fetchall()}
    return DashboardOut(
        products_in_stock=sum(r["on_hand"] > 0 for r in stock),
        low_stock=sum(0 < r["on_hand"] <= r["min_qty"] for r in stock),
        out_of_stock=sum(r["on_hand"] == 0 for r in stock),
        receipts=counts.get("receive", OpCounts()),
        deliveries=counts.get("delivery", OpCounts()),
        transfers=counts.get("transfer", OpCounts()),
        alerts=[StockAlert.model_validate(r) for r in stock if r["on_hand"] <= r["min_qty"]],
    )


# --- routes ---

router = APIRouter(tags=["inventory"])
any_role = require_role("staff", "manager")
manager_only = require_role("manager")


def _insert(table: str, cols: tuple[str, ...], values: tuple):
    with db.tx() as cur:
        return cur.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING *", values
        ).fetchone()


def _select_all(sql: str, params: list | tuple = ()):
    with db.tx() as cur:
        return cur.execute(sql, params).fetchall()


@router.get("/warehouses", response_model=list[WarehouseOut])
def get_warehouses(_: dict = Depends(current_user)):
    return _select_all("SELECT * FROM warehouses ORDER BY name")


@router.post("/warehouses", status_code=201, response_model=WarehouseOut)
def post_warehouse(body: WarehouseIn, _: dict = Depends(manager_only)):
    return _insert("warehouses", ("name", "short_code", "address"), (body.name, body.short_code, body.address))


@router.get("/locations", response_model=list[LocationOut])
def get_locations(warehouse_id: int | None = None, _: dict = Depends(current_user)):
    clause, params = db.where({"warehouse_id": warehouse_id})
    return _select_all(f"SELECT * FROM locations {clause} ORDER BY name", params)


@router.post("/locations", status_code=201, response_model=LocationOut)
def post_location(body: LocationIn, _: dict = Depends(manager_only)):
    return _insert("locations", ("warehouse_id", "name", "short_code"), (body.warehouse_id, body.name, body.short_code))


@router.get("/dashboard", response_model=DashboardOut)
def get_dashboard(warehouse_id: int | None = None, category: str | None = None, _: dict = Depends(current_user)):
    return dashboard(warehouse_id, category)


@router.get("/products", response_model=list[ProductOut])
def get_products(category: str | None = None, q: str | None = None, _: dict = Depends(current_user)):
    clause, params = db.where({"p.category": category}, _search(q))
    return _select_all(f"SELECT * FROM products p {clause} ORDER BY sku", params)


@router.post("/products", status_code=201, response_model=ProductOut)
def post_product(body: ProductIn, user: dict = Depends(manager_only)):
    return create_product(body, user)


@router.patch("/products/{product_id}", response_model=ProductOut)
def patch_product(product_id: int, body: ProductPatch, _: dict = Depends(manager_only)):
    return update_product(product_id, body)


@router.get("/quants", response_model=list[QuantOut])
def get_quants(warehouse_id: int | None = None, product_id: int | None = None, category: str | None = None,
               location_id: int | None = None, q: str | None = None, _: dict = Depends(current_user)):
    return list_quants(warehouse_id, product_id, category, location_id, q)


@router.get("/operations", response_model=list[OperationOut])
def get_operations(type: OpType | None = None, status: OpStatus | None = None, warehouse_id: int | None = None,
                   category: str | None = None, location_id: int | None = None, q: str | None = None,
                   limit: int = Query(200, ge=1, le=500), _: dict = Depends(current_user)):
    return list_operations(type, status, warehouse_id, category, location_id, q, limit)


@router.get("/operations/{op_id}", response_model=OperationDetail)
def get_operation_detail(op_id: int, _: dict = Depends(current_user)):
    return get_operation(op_id)


@router.post("/operations", status_code=201, response_model=OperationOut)
def post_operation(body: OperationIn, user: dict = Depends(any_role),
                   idempotency_key: str | None = Header(None, min_length=8, max_length=100)):
    op, created = create_operation(body, user, idempotency_key)
    if created:
        publish({"type": "operation.created", "operationId": op.id, "newState": op.status})
    return op


@router.post("/operations/{op_id}/transition", response_model=OperationOut)
def post_transition(op_id: int, body: TransitionIn, user: dict = Depends(any_role),
                    idempotency_key: str = Header(..., min_length=8, max_length=100)):
    op, changed = transition_operation(op_id, body.from_, body.to, user, idempotency_key)
    if changed:
        publish({"type": "operation.transitioned", "operationId": op.id, "newState": op.status})
    return op


@router.get("/moves", response_model=list[MoveOut])
def get_moves(type: OpType | None = None, status: OpStatus | None = None, warehouse_id: int | None = None,
              location_id: int | None = None, product_id: int | None = None, q: str | None = None,
              limit: int = Query(500, ge=1, le=5000), _: dict = Depends(current_user)):
    return list_moves(type, status, warehouse_id, location_id, product_id, q, limit)


@router.get("/ledger", response_model=list[LedgerOut])
def get_ledger(product_id: int | None = None, location_id: int | None = None, limit: int = Query(500, ge=1, le=5000),
               _: dict = Depends(manager_only)):
    clause, params = db.where({"product_id": product_id, "location_id": location_id})
    return _select_all(f"SELECT * FROM ledger {clause} ORDER BY id DESC LIMIT %s", (*params, limit))
