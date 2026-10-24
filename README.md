# TGTG Buddy

Single-user Too Good To Go companion: smart release-window polling, optional Adyen card pay, SMTP alerts, Tailscale-only UI.

Runs as one FastAPI process with JSON files under `data/` (no database). Secrets live in Fernet-encrypted `data/secrets.enc`.

> Automated ordering may violate TGTG terms of service. Keep **dry-run** on until you understand the reserve/abort flow.

## Requirements

- Python 3.12+ (local) or Docker on an **ARM64** (or amd64) host
- Tailscale on the VM, phone, and laptop
- Env `TGTG_BUDDY_MASTER_KEY` (Fernet key)

Short checklist:

1. Tailscale on VM + phone/laptop; `tailscale ip -4` → `TAILSCALE_IP` in `.env`
2. Docker on the ARM64 host
3. Copy `.env.example` → `.env`; set `TAILSCALE_IP` and `TGTG_BUDDY_MASTER_KEY`
4. `docker compose up -d --build`
5. Open `http://<tailscale-ip>:8321` from the Tailnet
6. Setup password → TGTG PIN → SMTP test → keep **dry-run** on

Generate a master key:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Local run (dev)

```bash
# From repo root
export TGTG_BUDDY_MASTER_KEY="$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")"
# Windows PowerShell:
# $env:TGTG_BUDDY_MASTER_KEY = (python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

uv sync --extra dev
uv run uvicorn app.main:app --host 127.0.0.1 --port 8321
```

Open http://127.0.0.1:8321 — data files land in `./data`.

## Tests

```bash
uv sync --extra dev
uv run pytest -q
```

## Layout

| Path | Role |
|------|------|
| `app/main.py` | FastAPI + HTMX UI + lifespan scheduler |
| `app/store.py` | Atomic JSON load/save |
| `app/secrets.py` | Fernet `secrets.enc` |
| `app/gate.py` | Rate-limited API worker |
| `app/scheduler.py` / `predictor.py` / `purchaser.py` | Core loop |
| `app/tgtg_client.py` / `adyen.py` | Client + CSE pay |
| `data/` | Bind-mounted state (gitignored) |
