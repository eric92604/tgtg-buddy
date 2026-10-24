"""BuddyClient — TgtgClient subclass with pay_order / get_payment_status."""

from __future__ import annotations

import json
import logging
from http import HTTPStatus
from typing import Any
from urllib.parse import urljoin

from tgtg import TgtgClient
from tgtg.exceptions import TgtgAPIError, TgtgLoginError

from app.adyen import encrypt_card_payload

logger = logging.getLogger(__name__)

PAY_ORDER_ENDPOINT = "order/v8/{}/pay"
PAYMENT_STATUS_ENDPOINT = "payment/v3/{}"
RETURN_URL = "adyencheckout://com.app.tgtg.itemview"


class BuddyClient(TgtgClient):
    """Extends upstream tgtg client with Adyen card payment helpers."""

    def __init__(self, *args, adyen_client_key: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.adyen_client_key = adyen_client_key
        self._pending_polling_id: str | None = None

    def auth_by_email(self, email: str) -> dict[str, Any]:
        """Start email login; returns {state, polling_id?} without blocking for PIN."""
        self.email = email
        response = self._post(
            self._get_url("auth/v5/authByEmail"),
            json={"device_type": self.device_type, "email": email},
        )
        if response.status_code != HTTPStatus.OK:
            if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
                raise TgtgAPIError(response.status_code, "Too many requests. Try again later.")
            raise TgtgLoginError(response.status_code, response.content)
        data = response.json()
        if data.get("state") == "WAIT":
            self._pending_polling_id = data.get("polling_id")
        return data

    def auth_by_pin(self, pin: str, polling_id: str | None = None) -> dict[str, Any]:
        """Complete PIN login and store tokens on the client."""
        pid = polling_id or self._pending_polling_id
        if not pid:
            raise TgtgLoginError(400, b"Missing polling_id")
        if not self.email:
            raise TgtgLoginError(400, b"Missing email")
        response = self._post(
            self._get_url("auth/v5/authByRequestPin"),
            json={
                "device_type": self.device_type,
                "email": self.email,
                "request_pin": pin.strip(),
                "request_polling_id": pid,
            },
        )
        if response.status_code != HTTPStatus.OK:
            raise TgtgLoginError(response.status_code, response.content)
        login_response = response.json()
        self.access_token = login_response["access_token"]
        self.refresh_token = login_response["refresh_token"]
        self.cookie = response.headers.get("Set-Cookie", "") or self.cookie
        from datetime import datetime

        self.last_time_token_refreshed = datetime.now()
        self._pending_polling_id = None
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "cookie": self.cookie,
        }

    def pay_order(
        self,
        order_id: str,
        card_data: dict[str, str],
        *,
        encrypted_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Pay a RESERVED order via Adyen CSE. card_data keys: number/card, cvv, month, year."""
        self.login()
        inner = encrypted_payload or encrypt_card_payload(
            card_data, client_key=self.adyen_client_key
        )
        # Match historical TGTG client escaping of forward slashes in the nested JSON.
        inner_payload_str = json.dumps(inner).replace("/", "\\/")
        payload = {
            "authorization": {
                "authorization_payload": {
                    "payload": inner_payload_str,
                    "payment_type": "CREDITCARD",
                    "save_payment_method": False,
                    "type": "adyenAuthorizationPayload",
                },
                "payment_provider": "ADYEN",
                "return_url": RETURN_URL,
            }
        }
        response = self._post(
            self._get_url(PAY_ORDER_ENDPOINT.format(order_id)),
            data=json.dumps(payload),
        )
        if response.status_code == HTTPStatus.OK:
            return response.json()
        raise TgtgAPIError(response.status_code, response.content)

    def get_payment_status(self, payment_id: str) -> dict[str, Any]:
        self.login()
        response = self._post(
            self._get_url(PAYMENT_STATUS_ENDPOINT.format(payment_id)),
            json={},
        )
        if response.status_code == HTTPStatus.OK:
            return response.json()
        raise TgtgAPIError(response.status_code, response.content)

    def persistable_credentials(self) -> dict[str, str | None]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "cookie": self.cookie,
            "email": self.email,
        }
