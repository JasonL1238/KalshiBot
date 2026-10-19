"""Which key type should the trading key be: Ed25519 or RSA-2048 (RSA-PSS)?

Run: python benchmarks/sign.py

Kalshi accepts both (docs/RESEARCH.md §1) and verifies with whichever public key is
registered. Each candidate runs `KalshiSigner.headers` for an order POST: timestamp,
message, signature, base64 (tests/test_auth.py checks that the signatures verify). Keys
are generated here and never leave the process. The script prints µs per signed
request, in a hot loop and after other work (as in a trading process, where signing is
occasional), then latency percentiles from an open-loop replay of order bursts (a
requote across many markets signs one request per order).
"""

from __future__ import annotations

import json
import statistics
import time

import harness
from cryptography.hazmat.backends.openssl.backend import backend
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from kalshibot.kalshi.auth import KalshiSigner

SIGNERS = {
    "Ed25519": KalshiSigner("bench", ed25519.Ed25519PrivateKey.generate()),
    "RSA-2048 PSS": KalshiSigner("bench", rsa.generate_private_key(65537, 2048)),
}
PATTERN = harness.Pattern(200, burst_every=0.5, burst_size=20)


def order_headers(signer: KalshiSigner):
    return lambda _: signer.headers("POST", harness.ORDER_PATH)


def in_context_us(sign, between, k: int = 300) -> float:
    """Median µs of one signature with `between()` run before each, untimed: what a
    signature costs when it isn't the only thing the process does."""
    times = []
    for _ in range(k):
        between()
        start = time.perf_counter_ns()
        sign(None)
        times.append(time.perf_counter_ns() - start)
    return statistics.median(times) / 1000


def main(n: int = 10_000, speed: float = 1.0) -> None:
    harness.header("sign: request signing for an order POST", ("cryptography",))
    print(f"OpenSSL: {backend.openssl_version_text()}")
    candidates = {name: order_headers(s) for name, s in SIGNERS.items()}
    frames = harness.Stream(100).frames
    contexts = {"after 1 ms idle": lambda: time.sleep(0.001),
                "after decoding 100 frames": lambda: [json.loads(f) for f in frames]}
    hot = harness.per_call_ns(candidates, [None] * 1_000)
    print(f"\n{'µs per signed request':<24}{'hot loop':>12}"
          + "".join(f"{c:>28}" for c in contexts))
    for name, fn in candidates.items():
        print(f"{name:<24}{hot[name] / 1000:>12.1f}" + "".join(
            f"{in_context_us(fn, between):>28.1f}" for between in contexts.values()))
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
