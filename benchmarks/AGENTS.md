# benchmarks/: execution performance evidence

Each script reproduces a decision recorded in [docs/EXECUTION.md](../docs/EXECUTION.md).
Results depend on the machine, so record platform and Python version next to any
numbers you publish (every script prints them first). `tests/test_benchmarks.py` runs
each script at a small scale and checks that its candidates agree.

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
| `gc_pauses.py` | GC policy: default vs `gc.freeze()` vs `gc.disable()`; runs on 3.14t (stdlib + msgspec) |
| `hot_path.py` | P1: the pure-Python hot path end to end, incumbent stack vs the fastest components |

## Rules for code here

- Measure open loop: latency runs from each item's *scheduled* time, so queueing behind
  a slow item is charged (`harness.open_loop`, `harness.Recorder`). Never time a
  closed loop and call it latency.
- Interleave candidates in rounds (`harness.compare`, `harness.replay`) and print the
  harness floor or sender lag and the host's CPU steal beside each table: tails on a
  shared VM are partly the hypervisor's.
- Network benchmarks use loopback only: the `Sender` and `OrderServer` processes. Keys
  are generated per run. Nothing here contacts Kalshi or places orders (root rule 7).
- Name scripts so they don't shadow the stdlib on `sys.path` (hence `gc_pauses.py`).

When a decision is final, delete the losing candidates' code (cite the pre-deletion
commit in EXECUTION.md) unless the script is the record that justifies the winner.
