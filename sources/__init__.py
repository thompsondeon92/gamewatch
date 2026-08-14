"""Game data sources: IGDB primary, RAWG fallback."""

from .catalog import Catalog
from .models import Game, describe_change, merge

__all__ = ["Catalog", "Game", "merge", "describe_change"]
