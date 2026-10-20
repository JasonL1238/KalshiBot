"""Near-zero-power rows: which leg `gauntlet.power`'s simulated worlds reject too often.
Per world: the real test's p_null and p_boot, and the simulated worlds' share rejecting by
the null alone, the bootstrap alone, and both (at α/12)."""
import sys, json
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
import numpy as np, pandas as pd
from kalshibot.kalshi.models import PRICE_SCALE, SIZE_SCALE
from kalshibot.research.validation import conformance as c, gauntlet, null, stats, synthetic

ROWS = {r[0]: r[2] for r in c.PREDICTION_GRID}
NAMES = {"binshock": "1¢ lower asks, binaries only, shock ρ = 0.5",
         "mixed": "mixed family (adds YES favorites): 1¢ lower NO asks, binaries only, shock ρ = 0.3, 100 events/day",
         "h1shock": "H1-shaped: 1¢ lower asks, NO at YES ≤ 20¢ only, half spread 0.5¢, shock ρ = 0.5 lasting days (φ = 0.8)"}
G = replace(c.GATES, power_worlds=200)

def legs(d, p, days, edge, rng, gates):
    """gauntlet.power, returning (null-only, boot-only, both) rejection shares."""
    dep, widen, totals = gauntlet._null(d, p, rng, gates)
    null_net = totals * np.sqrt(widen)
    win, lo, hi = gauntlet._alternative(p, edge)
    scale = np.sqrt(max(null.widening(dd, p.buy_yes, p.count, hi - lo, lo, hi, p.cluster, p.day, win)
                        for dd in (dep, null.Dependence(dep.rho, ()))))
    stake = p.count * float(PRICE_SCALE)
    day = np.searchsorted(days, p.day)
    expected = np.bincount(day, win * stake - p.paid, len(days))
    yes = null.draw_outcomes(lo, hi, p.cluster, gates.power_worlds, rng)
    net = np.where(p.buy_yes, yes, ~yes) * stake - p.paid
    daily = np.zeros((len(net), len(days))); np.add.at(daily.T, day, net.T)
    daily = expected + scale * (daily - expected)
    pb = np.array([stats.bootstrap_p_value(r, gates.power_resamples, rng) for r in daily])
    pn = null.p_value(daily.sum(axis=1), null_net)
    lv = gates.level
    skew = float(pd.Series(daily.ravel() - np.tile(expected, len(daily))).skew())
    return float(np.mean(pn <= lv)), float(np.mean(pb <= lv)), float(np.mean(np.maximum(pn, pb) <= lv)), skew

def one(args):
    seed, key = args
    kw = dict(ROWS[NAMES[key]])
    rng = np.random.default_rng(seed)
    # one_world's steps, keeping the positions
    side, quotes = kw.pop("side", "no"), "boundary"
    favorites = kw.pop("favorites", False); thresholds = kw.pop("thresholds", None)
    epd = kw.pop("events_per_day", 20)
    seam, days = 400, 500
    df = c._world(rng, seam, 100, epd, side, quotes, None, None, **kw)
    df = null.inputs(df.assign(priced_at=c._priced_at(df)), G.devig)
    disc, conf = df[df["day"] < seam], df[df["day"] >= seam]
    configs = c._configs(side, favorites, None, thresholds)
    ret = pd.DataFrame({k[0]: c._daily(c._positions(disc, k), range(seam)) for k in configs})
    chosen = next(k for k in configs if k[0] == gauntlet.select(ret))
    pos = c._positions(conf, chosen)
    conf_ret = c._daily(pos, range(seam, days))
    every = gauntlet.pooled([c._positions(disc, k) for k in configs])
    d, p = gauntlet.Positions.from_frame(every), gauntlet.Positions.from_frame(pos)
    v = gauntlet.evaluate(ret, d, conf_ret, p, rng, G)
    real_ret = conf_ret.to_numpy()
    edge = round(float(pos["expected"].mean()) / SIZE_SCALE)
    sn, sb, both, skew = legs(d, p, np.arange(seam, days), edge, rng, G)
    real_skew = float(pd.Series(real_ret - real_ret.mean()).skew())
    return {"real_null": v.p_null <= G.level, "real_boot": v.p_boot <= G.level,
            "real_both": v.p_value <= G.level, "sim_null": sn, "sim_boot": sb, "sim_both": both,
            "sim_skew": skew, "real_skew": real_skew, "widen": v.widening}

if __name__ == "__main__":
    key, n = sys.argv[1], int(sys.argv[2])
    with ProcessPoolExecutor() as pool:
        out = list(pool.map(one, [(s, key) for s in np.random.SeedSequence(33).spawn(n)]))
    json.dump(out, open(f"pdiag-{key}.json", "w"))
    a = lambda k: np.array([x[k] for x in out], dtype=float)
    print(f"{key} n={n}: real null {a('real_null').mean():.3f} boot {a('real_boot').mean():.3f} both {a('real_both').mean():.3f} | "
          f"simulated null {a('sim_null').mean():.4f} boot {a('sim_boot').mean():.4f} both {a('sim_both').mean():.4f} | "
          f"daily skew real {np.median(a('real_skew')):.2f} simulated {np.median(a('sim_skew')):.2f} | widen {a('widen').mean():.2f}")
