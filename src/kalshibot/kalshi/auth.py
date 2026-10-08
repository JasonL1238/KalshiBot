"""Kalshi API request signing.

Every authenticated request carries three headers:

    KALSHI-ACCESS-KEY        your API key id
    KALSHI-ACCESS-TIMESTAMP  current time in milliseconds
    KALSHI-ACCESS-SIGNATURE  base64( sign( timestamp + METHOD + path ) )

`path` is the full URL path including the `/trade-api/v2` prefix and excluding the
query string. Kalshi holds your public key; only you hold the private key, so a valid
signature proves the request came from you, and the timestamp stops replays.

Two key types are supported. Kalshi picks the verification algorithm from the public
key you registered, so we just sign with whatever kind of key we're given:
- Ed25519 (the default for new keys since Oct 2026): signs the message directly.
- RSA-2048: RSA-PSS with SHA-256, MGF1(SHA-256), salt length = digest length.
"""

from __future__ import annotations

import base64
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

PrivateKey = ed25519.Ed25519PrivateKey | rsa.RSAPrivateKey


class KalshiSigner:
    def __init__(self, key_id: str, private_key: PrivateKey):
        if not isinstance(private_key, ed25519.Ed25519PrivateKey | rsa.RSAPrivateKey):
            raise TypeError("Kalshi API keys must be Ed25519 or RSA private keys")
        self.key_id = key_id
        self._key = private_key

    @classmethod
    def from_pem(cls, key_id: str, pem: bytes) -> KalshiSigner:
        # The PEM header doesn't reliably tell you the key type; the parsed object does.
        return cls(key_id, serialization.load_pem_private_key(pem, password=None))

    @property
    def key_type(self) -> str:
        return "ed25519" if isinstance(self._key, ed25519.Ed25519PrivateKey) else "rsa"

    def sign(self, message: bytes) -> str:
        if isinstance(self._key, ed25519.Ed25519PrivateKey):
            signature = self._key.sign(message)
        else:
            signature = self._key.sign(
                message,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                            salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )
        return base64.b64encode(signature).decode()

    def headers(self, method: str, path: str, timestamp_ms: int | None = None) -> dict[str, str]:
        ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
        path = path.split("?", 1)[0]
        message = f"{ts}{method.upper()}{path}".encode()
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self.sign(message),
        }
