"""R against the candidate's variance: same-contract pairs exact, cross-contract pairs
the null's."""

import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

from kalshibot.research.validation import conformance as c
from kalshibot.research.validation import null

ROWS = {
    "shock": {**c.H1_COUPLED, "shock": 0.5, "persistence": 0.8},
    "plain": c.H1_COUPLED,
    "h1": c.H1_SHAPED,
    "strikes": {**c.H1_COUPLED, "strike_share": 1.0},
    "binaries": {**c.H1_COUPLED, "bucket_share": 0.0},
}


def candidate_var(pos):
    buy_yes = (pos["side"] == "yes").to_numpy()
    count = pos["fill_count"].to_numpy(float)
    b = (
        pos["paid"].to_numpy() / (count * 1_000_000 / 10_000 * 100)
        if False
        else pos["paid"].to_numpy() / (count * 10_000)
    )
    cl = null._clusters(
        buy_yes,
        count,
        pos["p_yes"].to_numpy(),
        pos["lo"].to_numpy(),
        pos["hi"].to_numpy(),
        pos["cluster"].to_numpy(),
        pos["day"].to_numpy(),
        b,
    )
    nested = cl["var_b"].sum()
    # same-contract part under the null, and exact
    sign = np.where(buy_yes, 1.0, -1.0) * count
    win = np.where(buy_yes, pos["p_yes"], 1 - pos["p_yes"]).astype(float)
    v = np.maximum(win * (1 - win), b * (1 - b))
    df = pd.DataFrame(
        index=pos.index,
        data={
            "cl": pos["cluster"].to_numpy(),
            "t": pos["ticker"].to_numpy(),
            "o": pos["priced_at"].astype("int64").to_numpy(),
            "s": sign,
            "v": v,
            "lo": pos["lo"].to_numpy(),
            "hi": pos["hi"].to_numpy(),
        },
    )
    ratio = (cl["boundary"] / cl["own"]).where(cl["own"] > 0, 1.0)
    df["ratio"] = df["cl"].map(ratio)
    df = df.sort_values(["cl", "t", "o"], kind="stable")
    before = df.groupby(["cl", "t"])["s"].cumsum() - df["s"]
    exact_same = (df["v"] * df["s"] * (df["s"] + 2 * before)).sum()
    null_same = 0.0
    for _, g in df.groupby(["cl", "t"]):
        a, h, w = g["lo"].to_numpy(), g["hi"].to_numpy(), g["s"].to_numpy()
        ov = np.clip(np.minimum.outer(h, h) - np.maximum.outer(a, a), 0, None)
        null_same += float(w @ (ov - np.outer(h - a, h - a)) @ w) * g["ratio"].iloc[0]
    # candidate B: exclusive events' cross-contract pairs at -p_a p_b (own prices),
    # not sliding overlap
    ex = pos["mutually_exclusive"].to_numpy(bool)
    df["ex"] = (
        ex[df.index.map(pos.index.get_loc)]
        if False
        else pd.Series(ex, index=pos.index).reindex(df.index).to_numpy()
        if False
        else ex[np.argsort(np.argsort(df.index))]
        if False
        else None
    )
    exf = pd.Series(ex).set_axis(pos.index)
    df["ex"] = exf.loc[df.index].to_numpy() if df.index.isin(pos.index).all() else False
    df["p"] = (
        pd.Series(pos["p_yes"].to_numpy(float), index=pos.index).loc[df.index].to_numpy()
        if df.index.isin(pos.index).all()
        else 0
    )
    cross_null = 0.0
    cross_b = 0.0
    for _, g in df[df["ex"]].groupby("cl"):
        a, h, w, p, t = (
            g["lo"].to_numpy(),
            g["hi"].to_numpy(),
            g["s"].to_numpy(),
            g["p"].to_numpy(),
            g["t"].to_numpy(),
        )
        diff = t[:, None] != t
        ov = np.clip(np.minimum.outer(h, h) - np.maximum.outer(a, a), 0, None)
        cross_null += float(w @ ((ov - np.outer(h - a, h - a)) * diff) @ w) * g["ratio"].iloc[0]
        cross_b += float(w @ (-np.outer(p, p) * diff) @ w) * g["ratio"].iloc[0]
    return nested, nested - null_same + exact_same - cross_null + cross_b


def one(args):
    seed, row = args
    kw = dict(ROWS[row])
    th = kw.pop("thresholds")
    rng = np.random.default_rng(seed)
    df = c._world(rng, 400, 100, 20, "no", "boundary", None, None, **kw)
    df = null.inputs(df.assign(priced_at=c._priced_at(df)), c.GATES.devig)
    conf = df[df["day"] >= 400]
    cfgs = c._configs("no", False, None, th)
    out = []
    for k in cfgs:
        pos = c._positions(conf, k)
        if len(pos) == 0:
            continue
        r, _ = c._coupling(pos, conf)
        nested, cand = candidate_var(pos)
        out.append((k[0], r, r * nested / cand))
    return out


if __name__ == "__main__":
    row, n = sys.argv[1], int(sys.argv[2])
    with ProcessPoolExecutor() as pool:
        res = [
            x
            for w in pool.map(one, [(s, row) for s in np.random.SeedSequence(5).spawn(n)])
            for x in w
        ]
    df = pd.DataFrame(res, columns=["config", "R", "R_cand"])
    print(row, df.groupby("config")[["R", "R_cand"]].agg(["mean", "max"]).round(3).to_string())
