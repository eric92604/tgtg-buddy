"""FastAPI app — routes + lifespan scheduler."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import config as config_mod
from app import secrets as secrets_mod
from app.auth import (
    AuthRedirectMiddleware,
    is_authenticated,
    is_first_run,
    login_user,
    logout_user,
)
from app.gate import ApiGate
from app.notify import notifier
from app.purchaser import load_orders
from app.scheduler import (
    WindowScheduler,
    load_watched,
    plan_phases_for_t,
    remove_watched,
    upsert_watched,
)
from app.store import ensure_data_dir
from app.tgtg_client import BuddyClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# In-memory stash for TGTG PIN flow (single-user).
_tgtg_pending: dict[str, str] = {}


def _flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session["flash"] = {"message": message, "kind": kind}


def _pop_flash(request: Request) -> dict[str, str] | None:
    return request.session.pop("flash", None)


def _ctx(request: Request, **extra: Any) -> dict[str, Any]:
    return {
        "request": request,
        "show_nav": is_authenticated(request) and not is_first_run(),
        "flash": _pop_flash(request),
        **extra,
    }


def _make_client() -> BuddyClient:
    tokens = secrets_mod.get_tgtg_tokens()
    cfg = config_mod.load_config()
    return BuddyClient(
        access_token=tokens.get("access_token"),
        refresh_token=tokens.get("refresh_token"),
        cookie=tokens.get("cookie"),
        email=tokens.get("email"),
        adyen_client_key=cfg.get("adyen_client_key") or None,
    )


def _fav_display(fav: dict[str, Any]) -> tuple[str, str, int, float | None]:
    item = fav.get("item") if isinstance(fav.get("item"), dict) else {}
    item_id = str(item.get("item_id") or fav.get("item_id") or "")
    name = item.get("name") or fav.get("display_name") or item_id
    store = (fav.get("store") or {}).get("store_name") or ""
    display = f"{store} — {name}" if store else str(name)
    available = int(fav.get("items_available") or item.get("items_available") or 0)
    price = None
    p = item.get("price_including_taxes") or fav.get("price_including_taxes")
    if isinstance(p, dict) and "minor_units" in p:
        decimals = int(p.get("decimals") or 2)
        price = int(p["minor_units"]) / (10**decimals)
    return item_id, display, available, price


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_data_dir()
    config_mod.ensure_config()
    gate = ApiGate(min_interval_s=1.0, daily_budget=2000)
    scheduler = WindowScheduler(
        gate,
        notify_send=notifier.send,
        client_factory=_make_client,
    )
    app.state.gate = gate
    app.state.scheduler = scheduler
    scheduler.start()
    logger.info("TGTG Buddy started (scheduler running)")
    yield
    await scheduler.stop()
    logger.info("TGTG Buddy stopped")


app = FastAPI(title="TGTG Buddy", lifespan=lifespan)

# Session signing key from master key when available; fallback for import-time tests.
try:
    _session_key = secrets_mod.session_secret_key()
except Exception:
    _session_key = "dev-insecure-session-key"

app.add_middleware(AuthRedirectMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_key,
    session_cookie="tgtg_buddy_session",
    same_site="lax",
    https_only=False,
    max_age=60 * 60 * 24 * 14,
)


@app.get("/health")
async def health():
    return {"ok": True, "service": "tgtg-buddy"}


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if is_first_run():
        return RedirectResponse("/setup", status_code=303)
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/favorites", status_code=303)


@app.get("/setup", response_class=HTMLResponse)
async def setup_get(request: Request):
    if not is_first_run():
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("setup.html", _ctx(request, show_nav=False))


@app.post("/setup")
async def setup_post(
    request: Request,
    password: str = Form(...),
    password2: str = Form(...),
):
    if not is_first_run():
        return RedirectResponse("/login", status_code=303)
    if password != password2:
        _flash(request, "Passwords do not match", "error")
        return RedirectResponse("/setup", status_code=303)
    if len(password) < 8:
        _flash(request, "Password must be at least 8 characters", "error")
        return RedirectResponse("/setup", status_code=303)
    secrets_mod.set_app_password(password)
    login_user(request)
    _flash(request, "Password set. Link your TGTG account next.", "ok")
    return RedirectResponse("/tgtg", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request):
    if is_first_run():
        return RedirectResponse("/setup", status_code=303)
    if is_authenticated(request):
        return RedirectResponse("/favorites", status_code=303)
    return templates.TemplateResponse("login.html", _ctx(request, show_nav=False))


@app.post("/login")
async def login_post(request: Request, password: str = Form(...)):
    if is_first_run():
        return RedirectResponse("/setup", status_code=303)
    if secrets_mod.verify_app_password(password):
        login_user(request)
        return RedirectResponse("/favorites", status_code=303)
    _flash(request, "Incorrect password", "error")
    return RedirectResponse("/login", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    logout_user(request)
    return RedirectResponse("/login", status_code=303)


@app.get("/tgtg", response_class=HTMLResponse)
async def tgtg_get(request: Request):
    tokens = secrets_mod.get_tgtg_tokens()
    return templates.TemplateResponse(
        "tgtg_login.html",
        _ctx(
            request,
            linked=bool(tokens.get("access_token")),
            email=tokens.get("email") or _tgtg_pending.get("email"),
            polling_id=_tgtg_pending.get("polling_id"),
        ),
    )


@app.post("/tgtg/email")
async def tgtg_email(request: Request, email: str = Form(...)):
    client = _make_client()
    try:
        data = client.auth_by_email(email.strip())
    except Exception as exc:
        _flash(request, f"TGTG email login failed: {exc}", "error")
        return RedirectResponse("/tgtg", status_code=303)
    state = data.get("state")
    if state == "WAIT":
        _tgtg_pending["email"] = email.strip()
        _tgtg_pending["polling_id"] = data.get("polling_id") or ""
        secrets_mod.set_tgtg_tokens(email=email.strip())
        _flash(request, "PIN sent — check your email (open on a device without the app).", "ok")
    elif state == "TERMS":
        _flash(request, "Email is not linked to a TGTG account.", "error")
    else:
        _flash(request, f"Unexpected state: {state}", "warn")
    return RedirectResponse("/tgtg", status_code=303)


@app.post("/tgtg/pin")
async def tgtg_pin(
    request: Request,
    pin: str = Form(...),
    polling_id: str = Form(""),
):
    email = _tgtg_pending.get("email") or secrets_mod.get_tgtg_tokens().get("email")
    pid = polling_id or _tgtg_pending.get("polling_id")
    if not email or not pid:
        _flash(request, "Request a PIN first.", "error")
        return RedirectResponse("/tgtg", status_code=303)
    client = BuddyClient(email=email)
    try:
        creds = client.auth_by_pin(pin, polling_id=pid)
        secrets_mod.set_tgtg_tokens(
            access_token=creds["access_token"],
            refresh_token=creds["refresh_token"],
            cookie=creds.get("cookie"),
            email=email,
        )
        _tgtg_pending.clear()
        _flash(request, "TGTG account linked.", "ok")
    except Exception as exc:
        _flash(request, f"PIN verification failed: {exc}", "error")
    return RedirectResponse("/tgtg", status_code=303)


@app.post("/tgtg/logout")
async def tgtg_logout(request: Request):
    secrets_mod.clear_tgtg_tokens()
    _tgtg_pending.clear()
    _flash(request, "TGTG tokens cleared.", "ok")
    return RedirectResponse("/tgtg", status_code=303)


@app.get("/favorites", response_class=HTMLResponse)
async def favorites_get(request: Request):
    watched = load_watched()
    watched_map = {str(w["item_id"]): w for w in watched}
    # Prefer last refreshed favorites cached in session; else show watched only.
    cached = request.session.get("favorites_cache") or []
    favorites = cached
    if not favorites and watched:
        favorites = [
            {
                "item_id": w["item_id"],
                "display_name": w.get("display_name") or w["item_id"],
                "items_available": w.get("items_available") or 0,
                "price": None,
            }
            for w in watched
        ]
    return templates.TemplateResponse(
        "favorites.html",
        _ctx(request, favorites=favorites, watched_map=watched_map),
    )


@app.post("/favorites/refresh")
async def favorites_refresh(request: Request):
    tokens = secrets_mod.get_tgtg_tokens()
    if not tokens.get("access_token"):
        _flash(request, "Link TGTG first.", "error")
        return RedirectResponse("/tgtg", status_code=303)
    client = _make_client()
    gate: ApiGate = request.app.state.gate
    try:
        raw = gate.call(lambda: client.get_favorites())
        creds = client.persistable_credentials()
        if creds.get("access_token"):
            secrets_mod.set_tgtg_tokens(**creds)
        favorites = []
        for fav in raw or []:
            item_id, display, available, price = _fav_display(fav)
            if not item_id:
                continue
            favorites.append(
                {
                    "item_id": item_id,
                    "display_name": display,
                    "items_available": available,
                    "price": price,
                }
            )
        request.session["favorites_cache"] = favorites
        _flash(request, f"Loaded {len(favorites)} favorites.", "ok")
    except Exception as exc:
        _flash(request, f"Refresh failed: {exc}", "error")
    return RedirectResponse("/favorites", status_code=303)


@app.post("/favorites/watch")
async def favorites_watch(
    request: Request,
    item_id: str = Form(...),
    display_name: str = Form(""),
    qty: int = Form(1),
    max_price: str = Form(""),
    action: str = Form("watch"),
):
    if action == "unwatch":
        remove_watched(item_id)
        _flash(request, "Stopped watching.", "ok")
        return RedirectResponse("/favorites", status_code=303)

    mp: float | None
    try:
        mp = float(max_price) if str(max_price).strip() else None
    except ValueError:
        mp = None
    existing = next((w for w in load_watched() if str(w.get("item_id")) == str(item_id)), {})
    upsert_watched(
        {
            **existing,
            "item_id": str(item_id),
            "display_name": display_name or existing.get("display_name") or item_id,
            "qty": max(1, int(qty)),
            "max_price": mp,
        }
    )
    _flash(request, "Watch list updated.", "ok")
    return RedirectResponse("/favorites", status_code=303)


@app.get("/upcoming", response_class=HTMLResponse)
async def upcoming_get(request: Request):
    now = datetime.now(timezone.utc)
    rows = []
    for w in load_watched():
        phases = []
        t = None
        if w.get("next_window"):
            try:
                text = str(w["next_window"])
                if text.endswith("Z"):
                    text = text[:-1] + "+00:00"
                t = datetime.fromisoformat(text)
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                phases = plan_phases_for_t(t, now)
            except ValueError:
                phases = []
        rows.append(
            {
                "display_name": w.get("display_name") or w.get("item_id"),
                "next_window": w.get("next_window"),
                "phases": phases,
            }
        )
    rows.sort(key=lambda r: r.get("next_window") or "9999")
    return templates.TemplateResponse("upcoming.html", _ctx(request, rows=rows))


@app.get("/orders", response_class=HTMLResponse)
async def orders_get(request: Request):
    orders = list(reversed(load_orders()))
    return templates.TemplateResponse("orders.html", _ctx(request, orders=orders))


@app.get("/settings", response_class=HTMLResponse)
async def settings_get(request: Request):
    cfg = config_mod.load_config()
    return templates.TemplateResponse(
        "settings.html",
        _ctx(
            request,
            cfg=cfg,
            smtp_password_set=bool(secrets_mod.get_smtp_password()),
            card_set=secrets_mod.card_configured(),
        ),
    )


@app.post("/settings")
async def settings_post(request: Request):
    form = await request.form()
    cfg = config_mod.load_config()
    cfg["dry_run"] = form.get("dry_run") == "1"
    cfg["pause"] = form.get("pause") == "1"
    try:
        cfg["daily_spend_cap"] = float(form.get("daily_spend_cap") or 0)
    except ValueError:
        cfg["daily_spend_cap"] = 0.0
    cfg["adyen_client_key"] = str(form.get("adyen_client_key") or "").strip()
    smtp = cfg.setdefault("smtp", {})
    smtp["host"] = str(form.get("smtp_host") or "").strip()
    try:
        smtp["port"] = int(form.get("smtp_port") or 587)
    except ValueError:
        smtp["port"] = 587
    smtp["username"] = str(form.get("smtp_username") or "").strip()
    smtp["from_addr"] = str(form.get("smtp_from") or "").strip()
    smtp["to_addr"] = str(form.get("smtp_to") or "").strip()
    smtp["use_tls"] = form.get("smtp_use_tls") == "1"
    config_mod.save_config(cfg)

    smtp_password = str(form.get("smtp_password") or "")
    if smtp_password:
        secrets_mod.set_smtp_password(smtp_password)

    number = str(form.get("card_number") or "").strip()
    cvv = str(form.get("card_cvv") or "").strip()
    month = str(form.get("card_month") or "").strip()
    year = str(form.get("card_year") or "").strip()
    holder = str(form.get("card_holder") or "").strip()
    if number or cvv or month or year or holder:
        kwargs: dict[str, str] = {}
        if number:
            kwargs["number"] = number
        if cvv:
            kwargs["cvv"] = cvv
        if month:
            kwargs["month"] = month
        if year:
            kwargs["year"] = year
        if holder:
            kwargs["holder"] = holder
        secrets_mod.set_card(**kwargs)

    _flash(request, "Settings saved.", "ok")
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/test-email")
async def settings_test_email(request: Request):
    ok = await notifier.send(
        "TGTG Buddy test email",
        "If you received this, SMTP is configured correctly.",
    )
    if ok:
        _flash(request, "Test email sent.", "ok")
    else:
        _flash(request, "Test email failed — check SMTP settings and logs.", "error")
    return RedirectResponse("/settings", status_code=303)


def create_app() -> FastAPI:
    return app
