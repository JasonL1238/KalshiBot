# H1 revision: diagnosis and candidates

Written before any candidate was implemented or run on an H1-shaped or third-table row.
Base: the maker revision (method-revision-makers at 447df3a).

## Problems (full scale at 4baee4e)

1. H1-shaped, NO at YES ≤ 20¢, half spread 0.5¢, shock ρ = 0.5 lasting days (φ = 0.8):
   gauntlet 6.0% at α = 0.05 (limit 5.97%); third table bold, 85.2 / 73.3 (92.9 / 84.1
   where the cross-day estimate is negative). The same row without the shock is at the
   edge too: widened null 6.3 / 0.9, gauntlet 5.5 / 0.7.
2. `gauntlet.power` bold in two near-zero-power taker rows: 0.7 / 0.0 ("1¢ lower asks,
   binaries only, shock ρ = 0.5") and 0.6 / 0.0 (the mixed family).

## Diagnosis of 1 (`diag-h1-shock.txt`, 200 worlds of the failing row)

| z from | var(z) | P(z > 1.645) | P(z > 2.64) |
|---|---|---|---|
| the estimated widening | 1.41 | 8.0% | 1.5% |
| an oracle dependence (1,500-day world, same settings) | 1.34 | 7.5% | 1.0% |
| the true coupling R | 1.22 | 6.0% | 1.0% |
| both | 1.16 | 5.5% | 0.5% |

Mean widening 1.305 estimated against 1.300 from the oracle dependence, with the same
ρ̂ (0.006 / 0.005) and cross-day (0.017 / 0.019). So the makers' Bartlett bias doesn't
appear here (cross-day dependence is a small part of H1's variance), and flat weights or
the jackknife have nothing to fix. The cause is the coupling of repeated decisions,
R = 1.16: the null nests or slides a contract's repeated positions on one draw, while in
truth they share one outcome.

What a revised within-cluster variance would leave, as R against it (8 worlds per row, all
configurations; `R` is against the current null):

| construction | shock row | no-shock row | H1 row, ≤ 20¢ | ≤ 10¢ | ≤ 3¢ | strike ladders | binaries |
|---|---|---|---|---|---|---|---|
| current (R) | 1.16 | 1.16 | 1.02 | 0.61 | 0.09 | 0.63 | 0.95 |
| A: same contract exact, v = max(null, break-even) | 0.61 | | 0.57 | 0.44 | 0.08 | 0.58 | 0.79 |
| B: A, plus a bucket pair at −p·p′ (own prices) | 0.85 | 0.86 | 0.77 | 0.55 | 0.09 | 0.58 | 0.79 |
| C: same contract at B_b(1 − B_b), a bucket pair at −p·p′ (own prices) | 1.02 | 1.02 | 1.05 | 1.02 | 0.55 | 0.64 | 0.98 |
| D: same contract at B_b(1 − B_b), a bucket pair at 0 | 0.72 | 0.72 | 0.73 | 0.85 | 0.49 | 0.64 | 0.98 |
| E: every pair at the later position's prices, buckets at −P·P′ | 1.06 | 1.06 | 1.11 | 1.10 | 1.24 | 0.77 | 1.00 |

- A and B take each pair's variance as the larger of the null's and the break-even's, as
  the maker null does. For H1's late, nearly settled positions the mid (half spread, the
  0.5% clip) is far more variable than the break-even, so they over-widen even binaries.
- E takes the null's prices at the later time, which for longshots sit above the truth, so
  −P·P′ is too negative and the variance too small: anti-conservative on every bucket row.
- C is near 1 on the failing rows but above 1 on the multi-cutoff H1 row: a bucket pair
  priced at the earlier position's own price overstates the negative term.
- D is never above 1 in these worlds, and can't be on buckets or single contracts: the
  same-contract term is the truth's (the later position's break-even variance, as Coupling
  R defines it), and a bucket pair's truth, −P·P′, is at most 0. On strike ladders it
  keeps the null's nested terms (the ladder rows' R: 0.64). Its cost: on bucket rows it
  over-widens by about a quarter to a third, so power falls.

## Diagnosis of 2 (`diag-power-legs.txt`, 40 worlds each)

| row | real null / bootstrap / both | power's simulated null / bootstrap / both |
|---|---|---|
| 1¢ lower asks, binaries, shock ρ = 0.5 | 2.5% / 22.5% / 2.5% | 0.56% / 12.5% / 0.56% |
| mixed family, shock ρ = 0.3 | 0% / 20% / 0% | 0.71% / 7.3% / 0.71% |
| H1-shaped, 1¢ lower, lasting shock | 70% / 75% / 70% | 84% / 95% / 84% |

In the two near-zero rows the null leg binds in both real and simulated worlds, and no
evidence shows the prediction high: here the real rate is above it, and with the full
run's 0 of 300 it is 1 in 340, consistent with about 0.6%. The cells are bold because of
the check. `conformance._calibration` takes the SE from the sample spread of the per-world
gap (predicted − found). When the gauntlet rejects in no world, found is constant, so the
spread holds only the predictions' own variation and none of the Bernoulli variance of the
outcome, and any positive prediction is bold. Under a calibrated prediction,
Var(found_i − predicted_i) = predicted_i (1 − predicted_i). For the binaries row that puts
2 SE at about 0.97 points, more than its 0.7-point gap. The H1 row's overprediction
(84% against 70%) is real, and comes from the coupling of 1.

## Candidates

All three touch only clusters holding a taker's repeated positions on one contract (more
than one decision time, not maker fills). One-decision takers and makers keep their exact
current path, so their rows stay byte-identical.

- **h1-D** (null): D above. Why it should work: the null's within-cluster variance is then
  at least the truth's term by term on binaries and buckets (R ≤ 1), so the widened null
  holds its size wherever the remaining dependence is estimated as well as now (the
  diagnosis: the dependence oracle changes little). `gauntlet.power` scales its worlds by
  the same widening, so its 84% should fall toward the measured rate. Expected cost: power
  on bucket rows (D over-widens them by a quarter to a third).
- **h1-C** (null, for comparison): C above. Expected: holds the failing rows (R about 1.02)
  with more power than D, but may break the multi-cutoff H1 row (R 1.05). Screened to
  measure what D's margin costs, not as the expected choice.
- **calibration-SE** (the check, for 2): `_calibration`'s SE as the larger of the sample
  one and the Bernoulli one, √(Σ p_i (1 − p_i)) / n. Why: under a calibrated prediction
  that is the gap's standard error, and it can't collapse when found is constant. It
  changes no test and no prediction, only which third-table cells are bold; on the
  4baee4e numbers it un-bolds the two near-zero rows and leaves the H1 row bold.

Because calibration-SE changes the pass rule in step 6, and h1-D or h1-C changes H1's
registered method, both need the user's sign-off.

## Recorded checks (after the screens)

The R table above was first computed in the session; `coupling-checks.txt` records the
same checks rerun (scripts in `scripts/`, seeded, so it reproduces every value above), plus
A's no-shock row (0.61), left blank above.

## Choice (after the screens)

Both candidates held every H1-shaped row at 0.25 and left no bold cell in the third table
(`h1-D-*`, `h1-C-*`); h1-C also held the shock row at full scale (`h1-C-shock-row-1.0.md`:
5.5 / 0.5 against 5.97 / 0.705). Chosen: **h1-D**. Its within-cluster variance is at
least the truth's term by term on binaries and buckets (R ≤ 1 by construction; at most
0.98 in every world checked), while h1-C's rests on prices at each position's own time
and exceeds 1 on the multi-cutoff H1 row (1.05), with thin margins at full scale (5.5%
against 5.97%) and at Holm's level on the no-shock row at 0.25 (0.8% against a full-scale
limit of 0.705%). The cost is H1 power: measured 26.7 against h1-C's 52.0 (0.75¢ row) and
54.7 against 70.7 (lasting shock), at 0.25. Which trade H1's registered method takes is
the user's call; h1-C's code is at ab2d5c6.
