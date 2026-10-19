# benchmarks/: execution performance evidence

Each script reproduces a decision recorded in [docs/EXECUTION.md](../docs/EXECUTION.md),
which also holds the M4 method (open-loop replay, coordinated-omission correction,
interleaved rounds, host noise) and every result. Results depend on the machine, so
record platform and Python version next to any numbers you publish (every script prints
them first). `tests/test_benchmarks.py` runs each script at a small scale, with replayed
frames checked byte for byte.

Setup: `pip install -e ".[dev,bench]"`. Run: `python benchmarks/<script>.py`.

| File | Decision it backs |
|------|-------------------|
| `book.py` | Order-book side structure: `kalshi.models.BookSide` (dict + price bitmap) |
| `harness.py` | Shared: schedules, open-loop timing, synthetic frames, replay sender, local order server |
| `decode.py` | JSON decoder for WebSocket frames (stdlib json, orjson, msgspec); `decode.Message` structs and `StructFeed` |
| `sign.py` | Trading key type: Ed25519 vs RSA-2048 PSS |
| `loop.py` | Event loop: asyncio vs uvloop, with a minimal reader to isolate the loop |
| `ws_clients.py` | WebSocket client: websockets vs aiohttp vs picows, on each loop |
| `http_clients.py` | Order HTTP client: httpx HTTP/1.1 and HTTP/2 vs aiohttp |
| `gc_pauses.py` | GC policy: default vs `gc.freeze()` vs `gc.disable()`, also on free-threaded builds |
| `hot_path.py` | P1: the pure-Python hot path end to end, incumbent stack vs the fastest components |

## Rules for code here

- Time latency open loop, from each item's scheduled time (`harness.open_loop`,
  `harness.Recorder`), never as a closed loop.
- Compare candidates in rotated rounds after a warm-up (`harness.compare`,
  `harness.replay`), and print the harness floor or sender lag and the host's CPU steal
  beside each table.
- Network benchmarks use loopback only: the `Sender` and `OrderServer` processes. Keys
  are generated per run. Nothing here contacts Kalshi or places orders (root rule 7).
- Name scripts so they don't shadow the stdlib on `sys.path` (hence `gc_pauses.py`).

When a decision is final, delete the losing candidates' code (cite the pre-deletion
commit in EXECUTION.md) unless the script is the record that justifies the winner.
