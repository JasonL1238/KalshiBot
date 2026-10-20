"""R against candidate C: each cluster's null variance with its off-diagonal terms replaced:
same contract, a before b: B_b(1 - B_b); two contracts of an exclusive event: -p_a p_b (own
prices); a ladder's: the null's (nested, anchored). The diagonal stays the null's, scaled."""
import sys
from concurrent.futures import ProcessPoolExecutor
import numpy as np, pandas as pd
from kalshibot.research.validation import conformance as c, null
from rb import ROWS

def cand(pos):
    buy_yes = (pos["side"] == "yes").to_numpy(); count = pos["fill_count"].to_numpy(float)
    b = pos["paid"].to_numpy() / (count * 10_000)
    p, lo, hi = (pos[k].to_numpy(float) for k in ("p_yes", "lo", "hi"))
    cl = null._clusters(buy_yes, count, p, lo, hi, pos["cluster"].to_numpy(), pos["day"].to_numpy(), b)
    ratio = (cl["boundary"] / cl["own"]).where(cl["own"] > 0, 1.0)
    s = np.where(buy_yes, 1.0, -1.0) * count
    ex = pos["mutually_exclusive"].to_numpy(bool); t = pos["ticker"].to_numpy()
    o = pos["priced_at"].astype("int64").to_numpy(); cid = pos["cluster"].to_numpy()
    total = 0.0
    for k, rows in pd.Series(np.arange(len(pos))).groupby(cid).indices.items():
        r = ratio.loc[k]
        if len(rows) == 1:
            total += cl.loc[k, "var_b"]; continue
        w, a, h, pp, tt, oo, bb = s[rows], lo[rows], hi[rows], p[rows], t[rows], o[rows], b[rows]
        ov = np.clip(np.minimum.outer(h, h) - np.maximum.outer(a, a), 0, None)
        C = (ov - np.outer(h - a, h - a)) * r  # the null's, scaled
        same = tt[:, None] == tt
        later = np.where(oo[:, None] >= oo, np.arange(len(rows))[:, None], np.arange(len(rows)))  # index of the later one
        Bl = bb[later]
        off = ~np.eye(len(rows), dtype=bool)
        C = np.where(same & off, Bl * (1 - Bl), C)
        if ex[rows[0]]:
            C = np.where(~same, -np.outer(pp, pp) * r, C)
        total += max(float(w @ C @ w), 0.0)
    return cl["var_b"].sum(), total

def one(args):
    seed, row = args
    kw = dict(ROWS[row]); th = kw.pop("thresholds")
    rng = np.random.default_rng(seed)
    df = c._world(rng, 400, 100, 20, "no", "boundary", None, None, **kw)
    df = null.inputs(df.assign(priced_at=c._priced_at(df)), c.GATES.devig)
    conf = df[df["day"] >= 400]
    out = []
    for k in c._configs("no", False, None, th):
        pos = c._positions(conf, k)
        if len(pos) == 0: continue
        r, _ = c._coupling(pos, conf)
        nested, cv = cand(pos)
        out.append((k[0], r, r * nested / cv))
    return out

if __name__ == "__main__":
    row, n = sys.argv[1], int(sys.argv[2])
    with ProcessPoolExecutor() as pool:
        res = [x for w in pool.map(one, [(s, row) for s in np.random.SeedSequence(5).spawn(n)]) for x in w]
    df = pd.DataFrame(res, columns=["config", "R", "R_cand"])
    print(row, df.groupby("config")[["R", "R_cand"]].agg(["mean", "max"]).round(3).to_string())
