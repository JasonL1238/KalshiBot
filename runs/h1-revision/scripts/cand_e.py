"""R against candidate E: the variance at prices, every pair at the later position's time
(conformance._coupling's R̄ with two buckets' terms at -P P' instead of 0), over the null's."""

import inspect
import sys
import textwrap
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd
from rb import ROWS

from kalshibot.research.validation import conformance as c
from kalshibot.research.validation import null

src = (
    textwrap.dedent(inspect.getsource(c._coupling))
    .replace("None if exclusive[rows[0]] else at_prices", "at_prices")
    .replace("def _coupling(", "def coupling_e(")
)
ns = {**vars(c)}
exec(src, ns)
coupling_e = ns["coupling_e"]


def one(args):
    seed, row = args
    kw = dict(ROWS[row])
    th = kw.pop("thresholds")
    rng = np.random.default_rng(seed)
    df = c._world(rng, 400, 100, 20, "no", "boundary", None, None, **kw)
    df = null.inputs(df.assign(priced_at=c._priced_at(df)), c.GATES.devig)
    conf = df[df["day"] >= 400]
    out = []
    for k in c._configs("no", False, None, th):
        pos = c._positions(conf, k)
        if len(pos) == 0:
            continue
        r, rbar = c._coupling(pos, conf)
        _, e = coupling_e(pos, conf)  # numerator at prices over the null's var_b
        out.append((k[0], r, rbar, r / e))
    return out


if __name__ == "__main__":
    row, n = sys.argv[1], int(sys.argv[2])
    with ProcessPoolExecutor() as pool:
        res = [
            x
            for w in pool.map(one, [(s, row) for s in np.random.SeedSequence(5).spawn(n)])
            for x in w
        ]
    df = pd.DataFrame(res, columns=["config", "R", "Rbar", "R_E"])
    print(row, df.groupby("config")[["R", "Rbar", "R_E"]].agg(["mean", "max"]).round(3).to_string())
