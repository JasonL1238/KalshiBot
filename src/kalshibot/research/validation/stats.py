"""Anti-overfitting statistics: how surprising is a backtest, given everything we tried?

- `psr` / `dsr`: Probabilistic and Deflated Sharpe Ratio (Bailey & López de Prado 2012,
  2014). DSR asks whether the best of N trials beats the Sharpe that N noise trials would
  reach by luck.
- `effective_trials`: N for DSR, from clustering the trials' return correlations (ONC,
  López de Prado 2019), so near-duplicate configs don't inflate N.
- `pbo`: Probability of Backtest Overfitting by combinatorially symmetric cross-validation
  (Bailey, Borwein, López de Prado & Zhu 2017).
- `bootstrap_p_value`: H0 mean ≤ 0 by the stationary bootstrap, for daily returns.
- `romano_wolf`: step-down adjusted p-values within a family, from jointly simulated null
  statistics (Romano & Wolf 2005). `holm`: family-wise correction across families.

Sharpe ratios here are per period (not annualized), computed from the return series given.
"""

from __future__ import annotations

import warnings
from itertools import combinations

import numpy as np
from scipy import stats


def sharpe(returns: np.ndarray) -> float | np.ndarray:
    """Per-period Sharpe of a series, or of each column of a T × N matrix (0 if flat)."""
    r = np.asarray(returns, dtype=float)
    if np.isnan(r).any():
        raise ValueError("returns contain NaN; put every config on one calendar, zero-filled")
    sd = r.std(axis=0, ddof=1)
    out = np.divide(r.mean(axis=0), sd, out=np.zeros_like(sd, dtype=float), where=sd > 0)
    return float(out) if out.ndim == 0 else out


def psr(returns: np.ndarray, benchmark_sr: float = 0.0) -> float:
    """P(true Sharpe > benchmark_sr), correcting for sample length, skew, and kurtosis."""
    r = np.asarray(returns, dtype=float)
    n, sr = len(r), sharpe(r)
    skew, kurt = stats.skew(r), stats.kurtosis(r, fisher=False)
    var = 1 - skew * sr + (kurt - 1) / 4 * sr**2
    if n < 3 or var <= 0:
        return float("nan")
    return float(stats.norm.cdf((sr - benchmark_sr) * np.sqrt(n - 1) / np.sqrt(var)))


def expected_max_sharpe(n_trials: float, trial_sr_variance: float) -> float:
    """Expected maximum Sharpe of n_trials independent zero-skill trials."""
    if n_trials <= 1:
        return 0.0
    z = stats.norm.ppf
    return float(np.sqrt(trial_sr_variance) * ((1 - np.euler_gamma) * z(1 - 1 / n_trials)
                                                + np.euler_gamma * z(1 - 1 / (n_trials * np.e))))


def dsr(returns: np.ndarray, n_trials: float, trial_sr_variance: float) -> float:
    """PSR against the Sharpe the best of `n_trials` noise trials would reach."""
    return psr(returns, expected_max_sharpe(n_trials, trial_sr_variance))


def effective_trials(trial_returns: np.ndarray, max_clusters: int | None = None) -> int:
    """Number of distinct strategies among the columns of `trial_returns` (T × N): k-means
    on the correlation distance, keeping the k with the best silhouette quality. This is
    the base step of ONC, without its recursive re-clustering of weak clusters."""
    from sklearn.cluster import KMeans
    from sklearn.exceptions import ConvergenceWarning
    from sklearn.metrics import silhouette_samples

    x = np.asarray(trial_returns, dtype=float)
    n = x.shape[1]
    if n <= 2:
        return n
    with np.errstate(invalid="ignore", divide="ignore"):  # flat trials correlate with nothing
        corr = np.nan_to_num(np.corrcoef(x, rowvar=False))
    dist = np.sqrt(np.clip((1 - corr) / 2, 0, None))
    best_k, best_q = 1, -np.inf
    for k in range(2, min(max_clusters or n - 1, n - 1) + 1):
        with warnings.catch_warnings():  # identical trials leave fewer distinct points than k
            warnings.simplefilter("ignore", ConvergenceWarning)
            labels = KMeans(k, n_init=10, random_state=0).fit_predict(dist)
        s = silhouette_samples(dist, labels)
        q = s.mean() / s.std() if s.std() > 0 else -np.inf
        if q > best_q:
            best_k, best_q = k, q
    return best_k


def pbo(performance: np.ndarray, n_blocks: int) -> float:
    """Probability that the in-sample best config ranks below the median out of sample.

    `performance` is T × N (periods × configs) of returns; T is cut into `n_blocks`
    contiguous blocks, and every half/half split of the blocks is tried."""
    m = np.asarray(performance, dtype=float)
    t, n = m.shape
    if n_blocks % 2 or n_blocks > t:
        raise ValueError("n_blocks must be even and at most the number of periods")
    blocks = np.array_split(np.arange(t), n_blocks)
    logits = []
    for chosen in combinations(range(n_blocks), n_blocks // 2):
        is_rows = np.concatenate([blocks[i] for i in chosen])
        oos_rows = np.concatenate([blocks[i] for i in range(n_blocks) if i not in chosen])
        best = np.argmax(sharpe(m[is_rows]))
        oos = sharpe(m[oos_rows])
        rank = stats.rankdata(oos)[best] / (n + 1)  # in (0, 1)
        logits.append(np.log(rank / (1 - rank)))
    return float(np.mean(np.array(logits) <= 0))


def bootstrap_p_value(returns: np.ndarray, n_draws: int, rng: np.random.Generator) -> float:
    """One-sided p-value for H0: mean return ≤ 0, by the stationary bootstrap (Politis &
    Romano 1994) with Politis & White's (2004) automatic block length, which keeps serial
    dependence. On daily returns it also absorbs any dependence between positions held
    on the same day."""
    from arch.bootstrap import StationaryBootstrap, optimal_block_length

    r = np.asarray(returns, dtype=float)
    if len(r) < 3:
        return 1.0
    if r.std() == 0:
        return 1 / (1 + n_draws) if r.mean() > 0 else 1.0
    block = float(optimal_block_length(r)["stationary"].iloc[0])
    block = block if np.isfinite(block) and block >= 1 else 1.0
    means = StationaryBootstrap(block, r, seed=rng).apply(np.mean, n_draws).ravel()
    return float((1 + np.sum(means - r.mean() >= r.mean())) / (1 + n_draws))


def romano_wolf(observed: np.ndarray, null_draws: np.ndarray) -> np.ndarray:
    """Step-down adjusted p-values for K statistics (larger = better), given B × K
    statistics simulated jointly under the null (same draws for every column)."""
    t = np.asarray(observed, dtype=float)
    draws = np.asarray(null_draws, dtype=float)
    order = np.argsort(-t)
    adjusted = np.empty_like(t)
    running = 0.0
    for step, k in enumerate(order):
        remaining = order[step:]
        p = float(np.mean(draws[:, remaining].max(axis=1) >= t[k]))
        running = max(running, p)  # step-down p-values are monotone
        adjusted[k] = running
    return adjusted


def holm(p_values: np.ndarray) -> np.ndarray:
    """Holm–Bonferroni adjusted p-values (family-wise error across families)."""
    p = np.asarray(p_values, dtype=float)
    m = len(p)
    order = np.argsort(p)
    adjusted = np.empty_like(p)
    running = 0.0
    for i, k in enumerate(order):
        running = max(running, min(1.0, (m - i) * p[k]))
        adjusted[k] = running
    return adjusted
