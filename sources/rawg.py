"""RAWG client (fallback source).

Simpler than IGDB - one API key, plain REST - but coarser release data: a
single `released` date plus a `tba` flag, with no notion of "Q3 2026". Used to
fill gaps IGDB leaves and to answer searches when IGDB is unavailable.

RAWG's free tier requires visible attribution with a backlink. /help and the
README carry it; keep it there if you fork this.
"""

import logging
from datetime import date

import httpx

from .models import Game

logger = logging.getLogger(__name__)

API_BASE = "https://api.rawg.io/api"
ATTRIBUTION = "Game data partly from RAWG — https://rawg.io"


def _parse_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def to_game(raw):
    released = _parse_date(raw.get("released"))
    if raw.get("tba"):
        human = "TBA"
    elif released:
        human = released.strftime("%d %b %Y")
    else:
        human = "TBD"

    platforms = []
    for entry in raw.get("platforms") or []:
        name = (entry.get("platform") or {}).get("name")
        if name:
            platforms.append(name)
    if not platforms:
        for entry in raw.get("parent_platforms") or []:
            name = (entry.get("platform") or {}).get("name")
            if name:
                platforms.append(name)

    slug = raw.get("slug") or ""
    return Game(
        source="rawg",
        source_id=str(raw["id"]),
        name=raw.get("name") or "Unknown",
        release_date=None if raw.get("tba") else released,
        release_human=human,
        platforms=platforms,
        url=f"https://rawg.io/games/{slug}" if slug else "",
        cover_url=raw.get("background_image") or "",
        summary=(raw.get("description_raw") or "")[:400],
    )


class RAWGClient:
    def __init__(self, api_key, client=None):
        self.api_key = (api_key or "").strip()
        self._http = client or httpx.AsyncClient(timeout=20)

    @property
    def enabled(self):
        return bool(self.api_key)

    async def aclose(self):
        await self._http.aclose()

    async def _get(self, path, **params):
        if not self.enabled:
            return None
        params["key"] = self.api_key
        resp = await self._http.get(f"{API_BASE}{path}", params=params)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    async def search_games(self, name, limit=8):
        try:
            data = await self._get("/games", search=name, page_size=limit)
        except httpx.HTTPError as exc:
            logger.warning("RAWG search failed for %r: %s", name, exc)
            return []
        if not data:
            return []
        return [to_game(row) for row in data.get("results", [])]

    async def best_match(self, name):
        """Closest single result, used to fill gaps in an IGDB record."""
        results = await self.search_games(name, limit=5)
        if not results:
            return None
        target = name.strip().lower()
        for game in results:
            if game.name.strip().lower() == target:
                return game
        return results[0]

    async def game_by_id(self, rawg_id):
        try:
            data = await self._get(f"/games/{rawg_id}")
        except httpx.HTTPError as exc:
            logger.warning("RAWG fetch failed for %s: %s", rawg_id, exc)
            return None
        return to_game(data) if data else None
