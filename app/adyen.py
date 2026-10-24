"""Vendored Adyen CSE encryptor (based on 66niko99/py-adyen-encrypt)."""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from os import urandom
from typing import Any
from urllib.parse import urljoin

import httpx
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

logger = logging.getLogger(__name__)

BASE_URL_ADYEN = "https://checkoutshopper-live.adyen.com/"
# Default TGTG live client key (from public tgtg payment reverse-engineering).
DEFAULT_ADYEN_CLIENT_KEY = "live_VPX45BIMLFAIVARYVKEDNC7OXIFBRQZ5"


class AdyenEncryptor:
    """Encrypt card fields for Adyen CSE payloads."""

    def __init__(
        self,
        adyen_public_key: str,
        adyen_version: str = "_0_1_1",
        adyen_prefix: str = "adyenjs",
    ) -> None:
        self.adyen_public_key = adyen_public_key
        self.adyen_version = adyen_version
        self.adyen_prefix = adyen_prefix

    def encrypt_field(self, name: str, value: str) -> str:
        plain = self.field_data(name, value)
        return self.encrypt_from_dict(plain)

    def encrypt_card(self, card: str, cvv: str, month: str, year: str) -> dict[str, str]:
        return {
            "card": self.encrypt_field("number", card),
            "cvv": self.encrypt_field("cvc", cvv),
            "month": self.encrypt_field("expiryMonth", month),
            "year": self.encrypt_field("expiryYear", year),
        }

    def field_data(self, name: str, value: str) -> dict[str, str]:
        generation_time = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return {name: value, "generationtime": generation_time}

    def encrypt_from_dict(self, dict_: dict[str, Any]) -> str:
        card_data_json_string = json.dumps(dict_, sort_keys=True)
        aes_key = self.generate_aes_key()
        nonce = self.generate_nonce()
        encrypted_card_data = self.encrypt_with_aes_key(
            aes_key, nonce, card_data_json_string.encode("utf-8")
        )
        encrypted_card_component = nonce + encrypted_card_data
        public_key = self.decode_adyen_public_key(self.adyen_public_key)
        encrypted_aes_key = self.encrypt_with_public_key(public_key, aes_key)
        return "{}{}${}${}".format(
            self.adyen_prefix,
            self.adyen_version,
            base64.standard_b64encode(encrypted_aes_key).decode(),
            base64.standard_b64encode(encrypted_card_component).decode(),
        )

    @staticmethod
    def decode_adyen_public_key(encoded_public_key: str):
        key_components = encoded_public_key.split("|")
        public_number = rsa.RSAPublicNumbers(
            int(key_components[0], 16),
            int(key_components[1], 16),
        )
        return public_number.public_key(default_backend())

    @staticmethod
    def encrypt_with_public_key(public_key, plaintext: bytes) -> bytes:
        return public_key.encrypt(plaintext, padding.PKCS1v15())

    @staticmethod
    def generate_aes_key() -> bytes:
        return AESCCM.generate_key(bit_length=256)

    @staticmethod
    def encrypt_with_aes_key(aes_key: bytes, nonce: bytes, plaintext: bytes) -> bytes:
        cipher = AESCCM(aes_key, tag_length=8)
        return cipher.encrypt(nonce, plaintext, None)

    @staticmethod
    def generate_nonce() -> bytes:
        return urandom(12)


def fetch_adyen_public_key(client_key: str | None = None) -> str:
    """Fetch Adyen CSE public key for the given client key."""
    key = (client_key or "").strip() or DEFAULT_ADYEN_CLIENT_KEY
    url = urljoin(BASE_URL_ADYEN, f"checkoutshopper/v1/clientKeys/{key}")
    headers = {
        "User-Agent": "TGTG/24.11.0 Dalvik/2.1.0 (Linux; Android 12; SM-G920V Build/MMB29K)",
        "Host": "checkoutshopper-live.adyen.com",
        "Connection": "Keep-Alive",
    }
    with httpx.Client(timeout=30.0) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
    public_key = data.get("publicKey")
    if not public_key:
        raise RuntimeError(f"Adyen clientKeys response missing publicKey: {data}")
    return public_key


def encrypt_card_payload(
    card_data: dict[str, str],
    *,
    client_key: str | None = None,
    public_key: str | None = None,
) -> dict[str, Any]:
    """Build the inner Adyen scheme payload with encrypted card fields."""
    key = public_key or fetch_adyen_public_key(client_key)
    enc = AdyenEncryptor(key, adyen_version="_0_1_1")
    card = enc.encrypt_card(
        card=card_data["number"] if "number" in card_data else card_data["card"],
        cvv=card_data["cvv"],
        month=card_data["month"],
        year=card_data["year"],
    )
    return {
        "type": "scheme",
        "encryptedCardNumber": card["card"],
        "encryptedExpiryMonth": card["month"],
        "encryptedExpiryYear": card["year"],
        "encryptedSecurityCode": card["cvv"],
        "threeDS2SdkVersion": "2.2.10",
    }
