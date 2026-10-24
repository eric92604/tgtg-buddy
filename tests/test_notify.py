from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app import config as config_mod
from app import secrets as secrets_mod
from app.notify import EmailNotifier


@pytest.mark.asyncio
async def test_send_success_mocked(data_dir: Path) -> None:
    cfg = config_mod.default_config()
    cfg["smtp"] = {
        "host": "smtp.example.com",
        "port": 587,
        "username": "u",
        "from_addr": "from@example.com",
        "to_addr": "to@example.com",
        "use_tls": True,
    }
    config_mod.save_config(cfg)
    secrets_mod.set_smtp_password("pw")

    notifier = EmailNotifier()
    with patch("app.notify.aiosmtplib.send", new_callable=AsyncMock) as send:
        ok = await notifier.send("subj", "body")
        assert ok is True
        send.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_never_raises(data_dir: Path) -> None:
    cfg = config_mod.default_config()
    cfg["smtp"] = {
        "host": "smtp.example.com",
        "port": 587,
        "username": "u",
        "from_addr": "from@example.com",
        "to_addr": "to@example.com",
        "use_tls": True,
    }
    config_mod.save_config(cfg)
    secrets_mod.set_smtp_password("pw")

    notifier = EmailNotifier()
    with patch("app.notify.aiosmtplib.send", new_callable=AsyncMock) as send:
        send.side_effect = RuntimeError("boom")
        ok = await notifier.send("subj", "body")
        assert ok is False


@pytest.mark.asyncio
async def test_send_skips_when_unconfigured(data_dir: Path) -> None:
    config_mod.save_config(config_mod.default_config())
    notifier = EmailNotifier()
    with patch("app.notify.aiosmtplib.send", new_callable=AsyncMock) as send:
        ok = await notifier.send("subj", "body")
        assert ok is False
        send.assert_not_called()
