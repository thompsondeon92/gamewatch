"""Facade over IGDB (primary) and RAWG (fallback).

Everything above this layer deals in `Game` objects and opaque keys like
"igdb:1020", and never needs to know which service answered.
"""

import asyncio
import logging

from .igdb import IGDBClient
from .models import merge
from .rawg import RAWGClient

logger = logging.getLogger(__name__)


def split_key(key):
    source, _, source_id = key.partition(":")
    return source, source_id


class Catalog:
    def __init__(self, igdb: IGDBClient, rawg: RAWGClient, enrich_concurrency=4):
        self.igdb = igdb
        self.rawg = rawg
        self._enrich_limit = asyncio.Semaphore(enrich_concurrency)

    @property
    def sources(self):
        active = []
        if self.igdb.enabled:
            active.append("igdb")
        if self.rawg.enabled:
            active.append("rawg")
        return active

    async def aclose(self):
        await asyncio.gather(
            self.igdb.aclose(), self.rawg.aclose(), return_exceptions=True
        )

    async def search(self, name, limit=8):
        """IGDB results, falling back to RAWG only when IGDB returns nothing."""
        results = await self.igdb.search_games(name, limit=limit)
        if results:
            return results
        if self.rawg.enabled:
            logger.info("IGDB had no results for %r, falling back to RAWG", name)
            return await self.rawg.search_games(name, limit=limit)
        return []

    async def enrich(self, game):
        """Fill an IGDB game's gaps from RAWG. Cheap no-op when nothing's missing."""
        if game.source != "igdb" or not self.rawg.enabled:
            return game
        if game.release_date is not None and game.platforms and game.cover_url:
            return game
        async with self._enrich_limit:
            other = await self.rawg.best_match(game.name)
        if other is None:
            return game
        # Only trust RAWG's date if the names really line up - RAWG search is
        # fuzzy and will happily return a sequel.
        if other.name.strip().lower() != game.name.strip().lower():
            other.release_date = None
        return merge(game, other)

    async def refresh(self, keys):
        """Current state for many keys at once -> {key: Game}."""
        igdb_ids, rawg_ids = [], []
        for key in keys:
            source, source_id = split_key(key)
            if source == "igdb":
                igdb_ids.append(source_id)
            elif source == "rawg":
                rawg_ids.append(source_id)

        games = {}

        if igdb_ids:
            for game in await self.igdb.games_by_ids(igdb_ids):
                games[game.key] = game

        if rawg_ids and self.rawg.enabled:
            # RAWG has no batch endpoint, so fan out with a concurrency cap.
            async def fetch(rawg_id):
                async with self._enrich_limit:
                    return await self.rawg.game_by_id(rawg_id)

            for game in await asyncio.gather(*(fetch(i) for i in rawg_ids)):
                if game is not None:
                    games[game.key] = game

        missing = set(keys) - set(games)
        if missing:
            logger.info("refresh: %d key(s) returned nothing: %s", len(missing), sorted(missing)[:5])
        return games

    async def resolve_filter(self, kind, text):
        return await self.igdb.resolve_filter(kind, text)

    async def discover(self, kind, value_id, limit=15):
        return await self.igdb.discover(kind, value_id, limit=limit)
