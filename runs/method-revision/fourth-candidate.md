# Fourth maker candidate: exact + flat cross-day weights

Written before the candidate was run on any maker row.

## Diagnosis (runs/method-revision/diag-exact.txt)

400 worlds of the failing row ("makers: NO bids 1–5¢ below the truth, 64 sweeps and
news, shock ρ = 0.5 lasting days (φ = 0.8)") under `exact`, each with an oracle widening
whose dependence comes from a 4,000-day world with the same settings:

| | estimated (400 days) | oracle (4,000 days) |
|---|---|---|
| mean widening | 11.47 | 13.90 |
| mean cross-day (Bartlett-weighted) | 0.67 | 0.87 |
| mean ρ̂ | 0.118 | 0.121 |
| var(z) | 1.11 | 0.86 |
| P(z > 1.645) | 0.045 | 0.030 |

With the oracle's dependence the widened null is conservative, so the null's model of a
maker's variance (with `exact`) is adequate. It fails because its cross-day term is
underestimated by about a quarter. That is a bias, not noise: ρ̂ is unbiased, and the
gap is in the cross-day sum.

## Mechanism

`null.widening` applies 2 Σ_{h ≤ H} (1 − h/(H+1)) c_h with H = ⌈√D⌉ = 20 for 400 days.
For an AR(1) shock, c_h ∝ φ^h, and at φ = 0.8:

- Σ φ^h (untruncated) = 4.00
- flat sum over h ≤ 20: 4 (1 − 0.8^20) = 3.95
- Bartlett-weighted, H = 20: 3.05; at the oracle's H = 64: 3.69

So the Bartlett weights alone cut the cross-day term by 3.05 / 3.69 = 0.83 against the
oracle (0.77 observed; the rest is the downward small-sample bias of removing each
sample's mean surprise). Bartlett weights keep a long-run variance estimate positive.
Here they buy nothing: `widening` already floors the total at the boundary variance.

Takers pass with the same estimator because their widening (about 3) is mostly
same-day and boundary variance. For the maker stress row the cross-day term is most of a
widening of about 12, so a quarter of it is the gap between 11.5 and 13.9.

## Candidate

`exact+flat`: `exact`, plus flat weights (each c_h counted once, h ≤ H) in the cross-day
sum of a dependence estimated from samples holding maker fills. Takers and H1-shaped
samples keep the Bartlett weights, so their results don't change.

Expected: the cross-day term rises by about 3.95 / 3.05 ≈ 1.3, which takes the mean
widening from 11.5 to about 14, near the oracle's, and holds the size. In worlds without
a lasting shock, c_h is near 0, so flat weights add noise but little widening on average
(a small power cost). Power's prediction uses the same widening, so it should stay
un-bold, as under `exact`.

Risk: the flat sum of 20 noisy c_h is noisier than the Bartlett sum. Worlds where it
comes out low still under-widen, so size may improve less than the mean suggests.
