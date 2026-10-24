"""Purchaser — rules, reserve, dry-run abort, live pay, 3DS keep-hold."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Callable

from app import config as config_mod
from app import secrets as secrets_mod
from app.store import load_json, save_json

logger = logging.getLogger(__name__)

ORDERS_FILE = "orders.json"
MAX_ORDERS = 100

# Payment / order states that imply 3DS or redirect challenge.
_THREEDS_HINTS = (
    "3DS",
    "THREEDS",
    "REDIRECT",
    "CHALLENGE",
    "IDENTIFY_SHOPPER",
    "CHALLENGE_SHOPPER",
    "PENDING",
    "AUTHORIZATION_INITIATED",
)


def load_orders() -> list[dict[str, Any]]:
    data = load_json(ORDERS_FILE, default=[])
    return data if isinstance(data, list) else []


def append_order_event(event: dict[str, Any]) -> None:
    rows = load_orders()
    event = dict(event)
    event.setdefault("ts", datetime.now(timezone.utc).isoformat())
    rows.append(event)
    save_json(ORDERS_FILE, rows[-MAX_ORDERS:])


def _price_from_item(item: dict[str, Any]) -> float | None:
    # Prefer item.item.price_including_taxes or order line style.
    candidates = [
        item.get("item", {}).get("price_including_taxes") if isinstance(item.get("item"), dict) else None,
        item.get("price_including_taxes"),
        item.get("item_price_including_taxes"),
    ]
    for price in candidates:
        if isinstance(price, dict) and "minor_units" in price:
            decimals = int(price.get("decimals") or 2)
            return int(price["minor_units"]) / (10**decimals)
        if isinstance(price, (int, float)):
            return float(price)
    return None


def _price_from_order(order: dict[str, Any]) -> float | None:
    line = order.get("order_line") or {}
    total = line.get("total_price_including_taxes") or line.get("item_price_including_taxes")
    if isinstance(total, dict) and "minor_units" in total:
        decimals = int(total.get("decimals") or 2)
        return int(total["minor_units"]) / (10**decimals)
    return None


def spent_today() -> float:
    today = date.today().isoformat()
    total = 0.0
    for row in load_orders():
        if not str(row.get("ts", "")).startswith(today):
            continue
        if row.get("result") in ("paid", "dry_run_ok", "reserved_3ds"):
            try:
                total += float(row.get("price") or 0)
            except (TypeError, ValueError):
                pass
    return total


def _looks_like_3ds(payload: Any) -> bool:
    text = str(payload).upper()
    return any(h in text for h in _THREEDS_HINTS) and (
        "ACTION" in text
        or "REDIRECT" in text
        or "3DS" in text
        or "THREEDS" in text
        or "CHALLENGE" in text
        or "IDENTIFY" in text
        or "AUTHORIZATION_INITIATED" in text
    )


class Purchaser:
    def __init__(
        self,
        *,
        gate_call: Callable[[Callable[[], Any]], Any],
        client_factory: Callable[[], Any],
        notify_send: Callable[[str, str], Any] | None = None,
    ) -> None:
        self.gate_call = gate_call
        self.client_factory = client_factory
        self.notify_send = notify_send

    def try_purchase(
        self,
        item: dict[str, Any],
        watched: dict[str, Any],
    ) -> dict[str, Any]:
        """Attempt reserve (+ pay or dry-run abort). Returns an event dict."""
        cfg = config_mod.load_config()
        item_id = str(watched.get("item_id") or self._extract_item_id(item) or "")
        display = watched.get("display_name") or item_id
        qty = int(watched.get("qty") or 1)
        max_price = watched.get("max_price")
        dry_run = bool(cfg.get("dry_run", True))

        event: dict[str, Any] = {
            "item_id": item_id,
            "display_name": display,
            "qty": qty,
            "dry_run": dry_run,
            "result": "skipped",
        }

        if cfg.get("pause"):
            event["reason"] = "paused"
            append_order_event(event)
            return event

        available = int(
            item.get("items_available")
            or (item.get("item") or {}).get("items_available")
            or 0
        )
        if available <= 0:
            event["reason"] = "no_stock"
            return event

        price = _price_from_item(item)
        if max_price is not None and price is not None and price > float(max_price):
            event["reason"] = "over_max_price"
            event["price"] = price
            append_order_event(event)
            return event

        cap = float(cfg.get("daily_spend_cap") or 0)
        if cap > 0 and price is not None and spent_today() + price > cap:
            event["reason"] = "daily_cap"
            event["price"] = price
            append_order_event(event)
            return event

        client = self.client_factory()

        # Skip if already ordered this item today (active orders).
        try:
            active = self.gate_call(lambda: client.get_active())
            if self._already_ordered_today(active, item_id):
                event["reason"] = "already_ordered_today"
                append_order_event(event)
                return event
        except Exception as exc:
            logger.warning("get_active failed: %s", exc)

        try:
            order = self.gate_call(lambda: client.create_order(item_id, qty))
        except Exception as exc:
            event["result"] = "reserve_failed"
            event["error"] = str(exc)
            append_order_event(event)
            return event

        order_id = order.get("id")
        order_price = _price_from_order(order) or price
        event["order_id"] = order_id
        event["price"] = order_price
        event["state"] = order.get("state")

        if dry_run:
            try:
                self.gate_call(lambda: client.abort_order(order_id))
                event["result"] = "dry_run_ok"
                event["reason"] = "reserved_then_aborted"
            except Exception as exc:
                event["result"] = "dry_run_abort_failed"
                event["error"] = str(exc)
            append_order_event(event)
            return event

        # Live pay
        if not secrets_mod.card_configured():
            event["result"] = "pay_skipped"
            event["reason"] = "no_card"
            try:
                self.gate_call(lambda: client.abort_order(order_id))
            except Exception:
                pass
            append_order_event(event)
            return event

        card = secrets_mod.get_card()
        card_data = {
            "number": card["number"] or "",
            "cvv": card["cvv"] or "",
            "month": card["month"] or "",
            "year": card["year"] or "",
        }
        try:
            payment = self.gate_call(lambda: client.pay_order(order_id, card_data))
            event["payment"] = {
                k: payment.get(k)
                for k in ("payment_id", "state", "payment_provider")
                if k in payment
            }
            payment_id = payment.get("payment_id")

            if _looks_like_3ds(payment):
                event["result"] = "reserved_3ds"
                event["reason"] = "3ds_keep_hold"
                self._notify_3ds(display, order_id)
                append_order_event(event)
                return event

            if payment_id:
                try:
                    status = self.gate_call(lambda: client.get_payment_status(payment_id))
                    event["payment_status"] = status.get("state")
                    if _looks_like_3ds(status):
                        event["result"] = "reserved_3ds"
                        event["reason"] = "3ds_keep_hold"
                        self._notify_3ds(display, order_id)
                        append_order_event(event)
                        return event
                    state = str(status.get("state") or "").upper()
                    if state in ("SUCCEEDED", "AUTHORIZED", "CAPTURED", "SUCCESS", "PAID"):
                        event["result"] = "paid"
                    elif state in ("FAILED", "CANCELLED", "REFUSED", "ERROR"):
                        event["result"] = "pay_failed"
                        event["error"] = state
                        try:
                            self.gate_call(lambda: client.abort_order(order_id))
                        except Exception:
                            pass
                    else:
                        # Unknown non-3DS state — keep hold and notify.
                        event["result"] = "reserved_pending"
                        event["reason"] = state
                except Exception as exc:
                    event["result"] = "status_failed"
                    event["error"] = str(exc)
            else:
                event["result"] = "paid" if not _looks_like_3ds(payment) else "reserved_3ds"
                if event["result"] == "reserved_3ds":
                    self._notify_3ds(display, order_id)
        except Exception as exc:
            event["result"] = "pay_failed"
            event["error"] = str(exc)
            if _looks_like_3ds(exc):
                event["result"] = "reserved_3ds"
                event["reason"] = "3ds_keep_hold"
                self._notify_3ds(display, order_id)
            else:
                try:
                    self.gate_call(lambda: client.abort_order(order_id))
                except Exception:
                    pass

        append_order_event(event)
        return event

    def _notify_3ds(self, display: str, order_id: str) -> None:
        if not self.notify_send:
            return
        try:
            result = self.notify_send(
                "TGTG Buddy: finish payment in app",
                f"{display} is reserved (order {order_id}). "
                "Open the official Too Good To Go app and complete 3DS/payment "
                "before the hold expires. Buddy will NOT abort this order.",
            )
            # Support both sync and async callables without forcing await here.
            if hasattr(result, "__await__"):
                import asyncio

                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.ensure_future(result)
                    else:
                        loop.run_until_complete(result)
                except Exception:
                    logger.exception("Failed scheduling 3DS email")
        except Exception:
            logger.exception("3DS notify failed")

    @staticmethod
    def _extract_item_id(item: dict[str, Any]) -> str | None:
        if item.get("item_id"):
            return str(item["item_id"])
        nested = item.get("item") or {}
        if isinstance(nested, dict) and nested.get("item_id"):
            return str(nested["item_id"])
        return None

    @staticmethod
    def _already_ordered_today(active: Any, item_id: str) -> bool:
        today = date.today().isoformat()
        orders = []
        if isinstance(active, dict):
            orders = active.get("orders") or active.get("active_orders") or []
        elif isinstance(active, list):
            orders = active
        for order in orders:
            if not isinstance(order, dict):
                continue
            oid = str(order.get("item_id") or (order.get("item") or {}).get("item_id") or "")
            if oid != str(item_id):
                continue
            reserved = str(order.get("reserved_at") or order.get("created_at") or "")
            if reserved.startswith(today) or not reserved:
                return True
        return False
