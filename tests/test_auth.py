import base64

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from kalshibot.kalshi.auth import KalshiSigner


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def verify(public_key, signature_b64: str, message: bytes) -> None:
    public_key.verify(
        base64.b64decode(signature_b64),
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_headers_sign_timestamp_method_and_path(key):
    signer = KalshiSigner("key-123", key)
    h = signer.headers("get", "/trade-api/v2/portfolio/balance", timestamp_ms=1700000000000)

    assert h["KALSHI-ACCESS-KEY"] == "key-123"
    assert h["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    verify(key.public_key(), h["KALSHI-ACCESS-SIGNATURE"],
           b"1700000000000GET/trade-api/v2/portfolio/balance")


def test_query_string_is_not_signed(key):
    signer = KalshiSigner("k", key)
    h = signer.headers("GET", "/trade-api/v2/portfolio/orders?limit=5", timestamp_ms=1)

    verify(key.public_key(), h["KALSHI-ACCESS-SIGNATURE"], b"1GET/trade-api/v2/portfolio/orders")
    with pytest.raises(InvalidSignature):
        verify(key.public_key(), h["KALSHI-ACCESS-SIGNATURE"],
               b"1GET/trade-api/v2/portfolio/orders?limit=5")


def test_ed25519_keys_sign_message_directly():
    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    signer = KalshiSigner.from_pem("k", pem)
    h = signer.headers("GET", "/trade-api/v2/portfolio/balance?x=1", timestamp_ms=7)

    assert signer.key_type == "ed25519"
    key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
                            b"7GET/trade-api/v2/portfolio/balance")


def test_from_pem_roundtrip(key):
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    signer = KalshiSigner.from_pem("k", pem)
    h = signer.headers("POST", "/trade-api/v2/portfolio/orders", timestamp_ms=42)
    verify(key.public_key(), h["KALSHI-ACCESS-SIGNATURE"], b"42POST/trade-api/v2/portfolio/orders")
