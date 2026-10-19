"""Strategies by hypothesis family (docs/STRATEGIES.md)."""

from kalshibot.strategies.base import Strategy
from kalshibot.strategies.deep_bids import DeepBids
from kalshibot.strategies.favorite_longshot import FavoriteLongshot

FAMILIES: dict[str, Strategy] = {s.family: s for s in (FavoriteLongshot(), DeepBids())}
