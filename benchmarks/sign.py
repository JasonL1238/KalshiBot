"""Which key type should the trading key be: Ed25519 or RSA-2048 (RSA-PSS)?

Run: python benchmarks/sign.py   (needs the [bench] extra)

Kalshi accepts both (docs/RESEARCH.md §1) and verifies with whichever public key is
registered. Each candidate runs `KalshiSigner.headers` for an order POST: timestamp,
message, signature, base64. Keys are generated here and never leave the process. The
script prints µs per signature, then latency percentiles from an open-loop replay of
order bursts (a requote across many markets signs one request per order).
"""

from __future__ import annotations

import base64

import harness
from cryptography.hazmat.backends.openssl.backend import backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from kalshibot.kalshi.auth import KalshiSigner

SIGNERS = {
    "Ed25519": KalshiSigner("bench", ed25519.Ed25519PrivateKey.generate()),
    "RSA-2048 PSS": KalshiSigner("bench", rsa.generate_private_key(65537, 2048)),
}
PATTERN = harness.Pattern(200, burst_every=0.5, burst_size=20)


def order_headers(signer: KalshiSigner):
    return lambda _: signer.headers("POST", harness.ORDER_PATH)


def check_signatures() -> None:
    """Each signer's headers verify against its public key."""
    for signer in SIGNERS.values():
        h = signer.headers("POST", harness.ORDER_PATH)
        message = f"{h['KALSHI-ACCESS-TIMESTAMP']}POST{harness.ORDER_PATH}".encode()
        signature, public = base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), signer._key.public_key()
        if signer.key_type == "ed25519":
            public.verify(signature, message)
        else:
            public.verify(signature, message, padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256())


def main(n: int = 6_000, speed: float = 1.0) -> None:
    harness.header("sign: request signing for an order POST", ("cryptography",))
    print(f"OpenSSL: {backend.openssl_version_text()}")
    check_signatures()
    candidates = {name: order_headers(s) for name, s in SIGNERS.items()}
    print(f"\n{'signed request':<24}{'µs each':>10}")
    for name, ns in harness.per_call_ns(candidates, [None] * 1_000).items():
        print(f"{name:<24}{ns / 1000:>10.1f}")
    pattern = PATTERN.faster(speed)
    offsets = harness.arrivals(n, pattern)
    with harness.Steal() as steal:
        lat = harness.compare({"(harness floor)": harness.noop, **candidates},
                              [None] * n, offsets)
    print(f"\nopen-loop replay ({pattern}), {n:,} orders, host CPU steal {steal}")
    print(harness.table_head("key type"))
    for name, ns in lat.items():
        print(harness.row(name, ns))


if __name__ == "__main__":
    main()
