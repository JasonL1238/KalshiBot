"""H1-shaped rows: z under the estimated widening, under an oracle dependence (from a
1,500-day world with the same settings), under the true coupling R, and under both."""

import json
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

from kalshibot.research.validation import conformance as c
from kalshibot.research.validation import gauntlet, null

ROWS = {"shock": {**c.H1_COUPLED, "shock": 0.5, "persistence": 0.8}, "plain": c.H1_COUPLED}
G = c.GATES


def sample(rng, days, kw, first=0):
    kw = dict(kw)
    thresholds = kw.pop("thresholds")
    df = c._world(rng, days, 1, kw.pop("events_per_day", 20), "no", "boundary", None, None, **kw)
    df = df[df["day"] < days]
    df = null.inputs(df.assign(priced_at=c._priced_at(df)), G.devig)
    configs = c._configs("no", False, None, thresholds)
    return df, configs


def one(args):
    seed, row = args
    kw = dict(ROWS[row])
    thresholds = kw.pop("thresholds")
    rng = np.random.default_rng(seed)
    seam = 400
    df = c._world(rng, seam, 100, 20, "no", "boundary", None, None, **kw)
    df = null.inputs(df.assign(priced_at=c._priced_at(df)), G.devig)
    disc, conf = df[df["day"] < seam], df[df["day"] >= seam]
    configs = c._configs("no", False, None, thresholds)
    chosen = configs[0] if len(configs) == 1 else None
    if chosen is None:
        ret = pd.DataFrame({k[0]: c._daily(c._positions(disc, k), range(seam)) for k in configs})
        chosen = next(k for k in configs if k[0] == gauntlet.select(ret))
    pos = c._positions(conf, chosen)
    d = gauntlet.Positions.from_frame(gauntlet.pooled([c._positions(disc, k) for k in configs]))
    p = gauntlet.Positions.from_frame(pos)
    dep, widen, totals = gauntlet._null(d, p, rng, G)
    long, _ = sample(seed.spawn(1)[0] and np.random.default_rng(seed.spawn(1)[0]), 1500, ROWS[row])
    o = gauntlet.Positions.from_frame(gauntlet.pooled([c._positions(long, k) for k in configs]))
    odep = null.dependence(
        o.won, o.buy_yes, o.count, o.p_yes, o.lo, o.hi, o.cluster, o.day, o.break_even
    )
    owiden = null.widening(
        odep, p.buy_yes, p.count, p.p_yes, p.lo, p.hi, p.cluster, p.day, p.break_even
    )
    r, rbar = c._coupling(pos, conf)
    sd, obs = totals.std(), float(p.net_pnl.sum() - pos["expected"].sum())
    def z(w):
        return obs / (sd * np.sqrt(w))
    return {
        "z_est": z(widen),
        "z_dep": z(owiden),
        "z_r": z(widen * r),
        "z_both": z(owiden * r),
        "widen": widen,
        "owiden": owiden,
        "R": r,
        "Rbar": rbar,
        "rho": dep.rho,
        "cross": dep.cross_day,
        "orho": odep.rho,
        "ocross": odep.cross_day,
        "p_boot_reject": None,
    }


if __name__ == "__main__":
    row, n = sys.argv[1], int(sys.argv[2])
    tasks = [(s, row) for s in np.random.SeedSequence(91).spawn(n)]
    with ProcessPoolExecutor() as pool:
        out = list(pool.map(one, tasks))
    json.dump(out, open(f"diag-{row}.json", "w"))
    def a(k):
        return np.array([x[k] for x in out])
    print(
        f"{row} n={n}: widen {a('widen').mean():.3f} oracle-dep widen {a('owiden').mean():.3f} "
        f"R {a('R').mean():.3f} R̄ {a('Rbar').mean():.3f} | "
        f"rho {a('rho').mean():.4f}/{a('orho').mean():.4f} "
        f"cross {a('cross').mean():.3f}/{a('ocross').mean():.3f}"
    )
    for k in ("z_est", "z_dep", "z_r", "z_both"):
        z = a(k)
        print(
            f"  {k:7s} var {z.var():.3f}  P(z>1.645) {np.mean(z > 1.645):.3f}  "
            f"P(z>2.638) {np.mean(z > 2.638):.4f}"
        )
