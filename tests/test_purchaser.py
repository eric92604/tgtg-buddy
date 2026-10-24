from __future__ import annotations

from pathlib import Path
from typing import Any

from app import config as config_mod
from app import secrets as secrets_mod
from app.purchaser import Purchaser, load_orders


class FakeClient:
    def __init__(self) -> None:
        self.orders_created = 0
        self.aborted: list[str] = []
        self.paid: list[str] = []
        self.active: dict[str, Any] = {"orders": []}
        self.pay_response: dict[str, Any] = {
            "payment_id": "pay-1",
            "state": "SUCCEEDED",
        }
        self.status_response: dict[str, Any] = {"state": "SUCCEEDED"}
        self.fail_pay = False
        self.threeds = False

    def get_active(self) -> dict[str, Any]:
        return self.active

    def create_order(self, item_id: str, qty: int) -> dict[str, Any]:
        self.orders_created += 1
        return {
            "id": f"ord-{self.orders_created}",
            "item_id": item_id,
            "state": "RESERVED",
            "order_line": {
                "total_price_including_taxes": {
                    "code": "EUR",
                    "minor_units": 599,
                    "decimals": 2,
                }
            },
        }

    def abort_order(self, order_id: str) -> None:
        self.aborted.append(order_id)

    def pay_order(self, order_id: str, card_data: dict[str, str]) -> dict[str, Any]:
        self.paid.append(order_id)
        if self.fail_pay:
            raise RuntimeError("pay exploded")
        if self.threeds:
            return {
                "payment_id": "pay-3ds",
                "state": "AUTHORIZATION_INITIATED",
                "action": {"type": "redirect", "url": "https://example/3ds"},
            }
        return self.pay_response

    def get_payment_status(self, payment_id: str) -> dict[str, Any]:
        return self.status_response


def _gate_call(fn):
    return fn()


def test_dry_run_reserves_then_aborts(data_dir: Path) -> None:
    config_mod.save_config({**config_mod.default_config(), "dry_run": True})
    client = FakeClient()
    p = Purchaser(gate_call=_gate_call, client_factory=lambda: client)
    item = {"item_id": "i1", "items_available": 2, "item": {"price_including_taxes": {"minor_units": 599, "decimals": 2}}}
    event = p.try_purchase(item, {"item_id": "i1", "qty": 1, "display_name": "Bag"})
    assert event["result"] == "dry_run_ok"
    assert client.orders_created == 1
    assert client.aborted == ["ord-1"]
    assert client.paid == []
    assert load_orders()[-1]["result"] == "dry_run_ok"


def test_skips_over_max_price(data_dir: Path) -> None:
    config_mod.save_config(config_mod.default_config())
    client = FakeClient()
    p = Purchaser(gate_call=_gate_call, client_factory=lambda: client)
    item = {
        "item_id": "i1",
        "items_available": 1,
        "item": {"price_including_taxes": {"minor_units": 1500, "decimals": 2}},
    }
    event = p.try_purchase(item, {"item_id": "i1", "qty": 1, "max_price": 5.0})
    assert event["result"] == "skipped"
    assert event["reason"] == "over_max_price"
    assert client.orders_created == 0


def test_live_3ds_keeps_hold_and_notifies(data_dir: Path) -> None:
    config_mod.save_config({**config_mod.default_config(), "dry_run": False})
    secrets_mod.set_card(number="4111111111111111", cvv="123", month="01", year="2030")
    client = FakeClient()
    client.threeds = True
    notes: list[tuple[str, str]] = []

    def notify(subject: str, body: str) -> bool:
        notes.append((subject, body))
        return True

    p = Purchaser(gate_call=_gate_call, client_factory=lambda: client, notify_send=notify)
    item = {"item_id": "i1", "items_available": 1}
    event = p.try_purchase(item, {"item_id": "i1", "qty": 1, "display_name": "Bag"})
    assert event["result"] == "reserved_3ds"
    assert client.aborted == []  # never abort on 3DS
    assert notes and "finish" in notes[0][0].lower()


def test_pay_failure_aborts(data_dir: Path) -> None:
    config_mod.save_config({**config_mod.default_config(), "dry_run": False})
    secrets_mod.set_card(number="4111111111111111", cvv="123", month="01", year="2030")
    client = FakeClient()
    client.fail_pay = True
    p = Purchaser(gate_call=_gate_call, client_factory=lambda: client)
    event = p.try_purchase(
        {"item_id": "i1", "items_available": 1},
        {"item_id": "i1", "qty": 1},
    )
    assert event["result"] == "pay_failed"
    assert client.aborted == ["ord-1"]
