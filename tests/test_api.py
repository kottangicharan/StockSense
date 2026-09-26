"""HTTP layer: cookies, RBAC, rate limits, OTP reset, idempotency header, realtime broadcast."""
import uuid

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def login(c: TestClient, login_id: str, password: str) -> None:
    r = c.post("/auth/login", json={"login_id": login_id, "password": password})
    assert r.status_code == 200, r.text


def test_signup_login_me_and_cookie_flags(client):
    r = client.post("/auth/signup", json={"login_id": "alice", "email": "Alice@Example.com", "password": "hunter2pass"})
    assert r.status_code == 201 and r.json()["role"] == "staff"
    assert client.post("/auth/signup", json={"login_id": "alice", "email": "x@y.io", "password": "hunter2pass"}).status_code == 409

    r = client.post("/auth/login", json={"login_id": "alice", "password": "hunter2pass"})
    assert r.status_code == 200
    assert "httponly" in r.headers["set-cookie"].lower()
    assert "token" not in r.json()  # tokens live only in httpOnly cookies
    assert client.get("/auth/me").json()["email"] == "alice@example.com"

    assert client.post("/auth/refresh").status_code == 200
    client.post("/auth/logout")
    assert client.get("/auth/me").status_code == 401


def test_login_rate_limited(client):
    client.post("/auth/signup", json={"login_id": "bob", "email": "bob@t.io", "password": "correct-pass"})
    codes = [client.post("/auth/login", json={"login_id": "bob", "password": "wrong-pass"}).status_code for _ in range(6)]
    assert codes == [401] * 5 + [429]


def test_otp_reset_flow(client, monkeypatch):
    sent = {}
    monkeypatch.setattr(auth, "send_otp", lambda email, otp: sent.update({email: otp}))
    client.post("/auth/signup", json={"login_id": "carol", "email": "carol@t.io", "password": "old-password"})

    same = {"message": "If that email is registered, an OTP has been sent"}
    assert client.post("/auth/forgot-password", json={"email": "nobody@t.io"}).json() == same
    assert client.post("/auth/forgot-password", json={"email": "carol@t.io"}).json() == same
    otp = sent["carol@t.io"]

    wrong = "000000" if otp != "000000" else "111111"
    r = client.post("/auth/reset-password", json={"email": "carol@t.io", "otp": wrong, "new_password": "new-password"})
    assert r.status_code == 400
    r = client.post("/auth/reset-password", json={"email": "carol@t.io", "otp": otp, "new_password": "new-password"})
    assert r.status_code == 200
    # Single use.
    r = client.post("/auth/reset-password", json={"email": "carol@t.io", "otp": otp, "new_password": "other-password"})
    assert r.status_code == 400

    assert client.post("/auth/login", json={"login_id": "carol", "password": "old-password"}).status_code == 401
    login(client, "carol", "new-password")


def test_otp_request_rate_limited(client):
    codes = [client.post("/auth/forgot-password", json={"email": "d@t.io"}).status_code for _ in range(6)]
    assert codes == [200] * 5 + [429]


def test_rbac_is_server_side(client, world):
    login(client, "sam", "staff-pass-1")
    assert client.post("/products", json={"sku": "X", "name": "X", "category": "Y"}).status_code == 403
    r = client.post("/operations", json={"type": "adjustment", "lines": [{"product_id": world["steel"], "qty": 1}],
                                         "source_location_id": world["stock"], "role": "manager"})
    assert r.status_code == 403
    assert client.get("/ledger").status_code == 403


def test_cross_site_write_blocked(client, world):
    login(client, "boss", "manager-pass-1")
    r = client.post("/warehouses", json={"name": "Evil", "short_code": "EV"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/warehouses", json={"name": "Good", "short_code": "GD"}, headers={"origin": "http://localhost:3000"})
    assert r.status_code == 201


def test_transition_idempotency_and_realtime(client, world):
    login(client, "boss", "manager-pass-1")
    op = client.post("/operations", json={"type": "receive", "lines": [{"product_id": world["steel"], "qty": 100}],
                                          "dest_location_id": world["stock"]}).json()

    with client.websocket_connect("/ws", headers={"cookie": f"access_token={client.cookies['access_token']}"}) as ws:
        # Idempotency-Key is mandatory on transitions.
        assert client.post(f"/operations/{op['id']}/transition", json={"from": "draft", "to": "waiting"}).status_code == 422

        for frm, to in [("draft", "waiting"), ("waiting", "ready")]:
            r = client.post(f"/operations/{op['id']}/transition", json={"from": frm, "to": to},
                            headers={"Idempotency-Key": uuid.uuid4().hex})
            assert r.status_code == 200
            assert ws.receive_json() == {"type": "operation.transitioned", "operationId": op["id"], "newState": to}

        key = uuid.uuid4().hex
        first = client.post(f"/operations/{op['id']}/transition", json={"from": "ready", "to": "done"},
                            headers={"Idempotency-Key": key})
        replay = client.post(f"/operations/{op['id']}/transition", json={"from": "ready", "to": "done"},
                             headers={"Idempotency-Key": key})
        assert first.status_code == replay.status_code == 200
        assert first.json() == replay.json()
        assert ws.receive_json()["newState"] == "done"

        # Same key, different request -> rejected rather than silently replayed.
        r = client.post(f"/operations/{op['id']}/transition", json={"from": "draft", "to": "waiting"},
                        headers={"Idempotency-Key": key})
        assert r.status_code == 422

    assert len(client.get(f"/operations/{op['id']}").json()["ledger"]) == 1
    quants = client.get("/quants", params={"warehouse_id": world["warehouse"]}).json()
    assert [(q["location_name"], q["qty"]) for q in quants] == [("WH/Stock", 100.0)]
    assert client.get("/operations", params={"status": "done", "category": "Raw Material"}).json()[0]["id"] == op["id"]


def test_ws_rejects_anonymous(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws") as ws:
        ws.receive_json()


def test_products_dashboard_and_filters(client, world):
    login(client, "boss", "manager-pass-1")
    r = client.post("/products", json={"sku": "CHAIR-1", "name": "Chair", "category": "Furniture", "uom": "Units",
                                       "min_qty": 10, "initial_qty": 8, "initial_location_id": world["stock"]})
    assert r.status_code == 201, r.text
    chair = r.json()
    assert (chair["uom"], chair["min_qty"]) == ("Units", 10.0)
    assert client.post("/products", json={"sku": "X", "name": "X", "category": "Y", "initial_qty": 1}).status_code == 422

    # Initial stock went through the ledger, so it's on hand and in Move History.
    assert [q["qty"] for q in client.get("/quants", params={"q": "chair"}).json()] == [8.0]
    assert client.get("/ledger", params={"product_id": chair["id"]}).json()[0]["delta"] == 8.0

    assert client.patch(f"/products/{chair['id']}", json={"min_qty": 5}).json()["min_qty"] == 5.0
    assert client.patch("/products/999999", json={"name": "Nope"}).status_code == 404

    client.post("/operations", json={"type": "receive", "lines": [{"product_id": world["steel"], "qty": 5}],
                                     "dest_location_id": world["stock"], "partner": "Acme Steel"})
    d = client.post("/operations", json={"type": "delivery", "lines": [{"product_id": chair["id"], "qty": 2}],
                                         "source_location_id": world["stock"]}).json()
    client.post(f"/operations/{d['id']}/transition", json={"from": "draft", "to": "canceled"},
                headers={"Idempotency-Key": uuid.uuid4().hex})

    dash = client.get("/dashboard").json()
    assert (dash["products_in_stock"], dash["low_stock"], dash["out_of_stock"]) == (1, 0, 1)
    assert dash["receipts"]["open"] == 1 and dash["deliveries"]["open"] == dash["transfers"]["open"] == 0
    assert [a["sku"] for a in dash["alerts"]] == ["STEEL"]
    assert client.get("/dashboard", params={"category": "Furniture"}).json()["alerts"] == []

    ops = client.get("/operations", params={"location_id": world["stock"], "status": "canceled"}).json()
    assert [o["id"] for o in ops] == [d["id"]]
    assert client.get("/operations", params={"location_id": world["rack"]}).json() == []
    assert client.get("/operations", params={"q": "STE"}).json()[0]["partner"] == "Acme Steel"
    assert [p["sku"] for p in client.get("/products", params={"q": "chai"}).json()] == ["CHAIR-1"]


def test_multi_line_receipt_delivery_references_and_moves(client, world):
    login(client, "boss", "manager-pass-1")
    stock, steel = world["stock"], world["steel"]
    desk = client.post("/products", json={"sku": "DESK001", "name": "Desk", "category": "Furniture",
                                          "unit_cost": 3000}).json()
    key = lambda: {"Idempotency-Key": uuid.uuid4().hex}  # noqa: E731

    r = client.post("/operations", json={"type": "receive", "dest_location_id": stock, "partner": "Azure Interior",
                                         "lines": [{"product_id": steel, "qty": 10}, {"product_id": desk["id"], "qty": 6}]})
    assert r.status_code == 201, r.text
    receipt = r.json()
    assert receipt["reference"] == "WH/IN/0001"
    assert receipt["responsible"] == "boss"
    assert [(line["sku"], line["qty"]) for line in receipt["lines"]] == [("STEEL", 10.0), ("DESK001", 6.0)]
    # Receipts skip 'waiting': draft -> ready -> done.
    client.post(f"/operations/{receipt['id']}/transition", json={"from": "draft", "to": "ready"}, headers=key())
    r = client.post(f"/operations/{receipt['id']}/transition", json={"from": "ready", "to": "done"}, headers=key())
    assert r.status_code == 200, r.text

    delivery = client.post("/operations", json={
        "type": "delivery", "source_location_id": stock, "partner": "Azure Interior", "delivery_address": "4 Market Rd",
        "scheduled_date": "2000-01-01",  # in the past -> late
        "lines": [{"product_id": steel, "qty": 4}, {"product_id": desk["id"], "qty": 6}]}).json()
    assert delivery["reference"] == "WH/OUT/0001"
    assert client.post("/operations", json={"type": "delivery", "source_location_id": stock, "lines": [
        {"product_id": steel, "qty": 1}, {"product_id": steel, "qty": 2}]}).status_code == 409  # duplicate product (DB unique)

    # Open delivery reserves stock: on hand stays, free-to-use drops.
    quants = {q["sku"]: q for q in client.get("/quants", params={"location_id": stock}).json()}
    assert (quants["DESK001"]["qty"], quants["DESK001"]["free_qty"], quants["DESK001"]["unit_cost"]) == (6.0, 0.0, 3000.0)
    assert (quants["STEEL"]["qty"], quants["STEEL"]["free_qty"]) == (10.0, 6.0)

    dash = client.get("/dashboard").json()["deliveries"]
    assert (dash["open"], dash["late"], dash["ready"]) == (1, 1, 0)

    moves = client.get("/moves", params={"q": "WH/OUT"}).json()
    assert [(m["sku"], m["from_location"], m["to_location"], m["qty"]) for m in moves] == [
        ("STEEL", "WH/Stock", "Azure Interior", 4.0), ("DESK001", "WH/Stock", "Azure Interior", 6.0)]
    assert {m["from_location"] for m in client.get("/moves", params={"type": "receive"}).json()} == {"Azure Interior"}
    assert [o["reference"] for o in client.get("/operations", params={"q": "azure"}).json()] == ["WH/OUT/0001", "WH/IN/0001"]

    # Validating the multi-line delivery posts one ledger row per line, atomically.
    for frm, to in [("draft", "waiting"), ("waiting", "ready"), ("ready", "done")]:
        assert client.post(f"/operations/{delivery['id']}/transition", json={"from": frm, "to": to},
                           headers=key()).status_code == 200
    detail = client.get(f"/operations/{delivery['id']}").json()
    assert sorted(entry["delta"] for entry in detail["ledger"]) == [-6.0, -4.0]
    assert client.get("/quants", params={"location_id": stock, "q": "desk"}).json()[0]["qty"] == 0.0

    # Staff can read Move History (the raw ledger stays manager-only).
    client.post("/auth/logout")
    login(client, "sam", "staff-pass-1")
    assert client.get("/moves").status_code == 200
