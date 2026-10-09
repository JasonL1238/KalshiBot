# Learning list

Short, free reads and videos, in study order. Total ≈ 5 hours. Links checked 2026-10-03.

## 1. How trading works: order books, spreads, market makers (~45 min)

| | What | Time | Why |
|-|------|------|-----|
| 📄 | [How do prediction markets work?](https://news.kalshi.com/p/how-prediction-markets-work) (Kalshi) | 5 min | Contracts pay $1 or $0; price ≈ probability |
| 📄 | [An Introduction to Limit Order Books](https://machow.ski/posts/2021-07-18-introduction-to-limit-order-books/) | 15 min | Bids, asks, matching, price-time priority, written for programmers |
| 🎥 | [What Exactly Do Market Makers Do?](https://www.youtube.com/watch?v=63BoycqDwIA) (Logically Answered) | 12 min | Who's on the other side of your trade and how they profit from the spread. The title is clickbait, but the mechanics are explained well |
| 📄 | [Kalshi: orderbook responses](https://docs.kalshi.com/getting_started/orderbook_responses) | 5 min | Why Kalshi shows only bids, and how `models.py` derives asks |
| 📄 | [Kalshi: fees](https://help.kalshi.com/en/articles/13823805-fees) | 5 min | The fee curve every trade has to beat |

**Try it:** run `kalshibot markets` and explain every column out loud.

## 2. Prediction markets specifically (~1.75 hr)

| | What | Time | Why |
|-|------|------|-----|
| 📄 | Wolfers & Zitzewitz, [Prediction Markets](https://www.aeaweb.org/articles?id=10.1257/0895330041371321) (2004) | 45 min | The classic, readable intro: why market prices aggregate information and where they fail |
| 📄 | [The economics of the Kalshi prediction market](https://cepr.org/voxeu/columns/economics-kalshi-prediction-market) (VoxEU) | 10 min | Favorite-longshot bias; makers beat takers. [Full paper](https://www.karlwhelan.com/Papers/Kalshi.pdf) is optional |
| 📄 | Becker, [Prediction market microstructure](https://www.jbecker.dev/research/prediction-market-microstructure) | 20 min | Who wins and loses on Kalshi, measured over 72M trades |
| 📄 | [Prediction Markets Beat the Weather Forecast on Tomorrow's High Temperature](https://arxiv.org/abs/2609.23969) (2026) | 30 min | The key paper for H5 (weather). Read the intro and results; it sets the bar |

## 3. Probability and bet sizing (~1 hr)

| | What | Time | Why |
|-|------|------|-----|
| 🎥 | [Bayes theorem, the geometry of changing beliefs](https://www.youtube.com/watch?v=HZGCoVF3YvM) (3Blue1Brown) | 15 min | Updating a probability when new info (a forecast) arrives |
| 🎥 | [The Kelly Criterion: how much to bet](https://www.youtube.com/watch?v=bXRYoOOFS94) (Zerodha) | 16 min | Bet sizing when you have an edge |
| 📄 | Haghani & Dewey, [Betting on a biased coin](https://arxiv.org/abs/1701.01427) | 20 min | 61 finance-trained people got a 60% coin and $25; ~30% went bust. Sizing matters as much as being right |

## 4. Probabilistic forecasting and ML evaluation (~1 hr)

| | What | Time | Why |
|-|------|------|-----|
| 📄 | [What is an ensemble forecast?](https://www.metoffice.gov.uk/research/weather/ensemble-forecasting/what-is-an-ensemble-forecast) (Met Office) | 5 min | How weather models express uncertainty, the main H5 feature |
| 📄 | [Brier score](https://en.wikipedia.org/wiki/Brier_score) (Wikipedia, first sections) | 10 min | Scores probability forecasts. Strategies themselves are judged by the gauntlet in BACKTESTING.md |
| 📄 | [Probability calibration](https://scikit-learn.org/stable/modules/calibration.html) (scikit-learn guide) | 25 min | Reliability diagrams + isotonic/Platt; you'll use this exact code |
| 📄 | [Evaluating distributional forecasts](https://otexts.com/fpp3/distaccuracy.html) (*Forecasting: Principles and Practice*) | 15 min | Quantile scores and CRPS for scoring a whole predicted distribution |

## 5. Not fooling yourself (~40 min, before M3)

| | What | Time | Why |
|-|------|------|-----|
| 📄 | López de Prado, [The 10 Reasons Most Machine Learning Funds Fail](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3104816) | 40 min | Backtest overfitting, leakage in cross-validation. Skim the parts on backtesting and CV |

## Later, when you get there

- **Market making (H11):** [Hummingbot's guide to Avellaneda–Stoikov](https://hummingbot.org/blog/guide-to-the-avellaneda--stoikov-strategy/)
- **Books, if you get hooked:** *Trading and Exchanges* (Larry Harris) for market structure,
  *Superforecasting* (Tetlock) for forecasting judgment, *Fortune's Formula* (Poundstone)
  for the story of Kelly betting.
