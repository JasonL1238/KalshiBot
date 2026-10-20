"""Strategies by hypothesis family (docs/STRATEGIES.md)."""

from kalshibot.strategies.base import Strategy
from kalshibot.strategies.deep_bids import DeepBids

FAMILIES: dict[str, Strategy] = {s.family: s for s in (DeepBids(),)}
