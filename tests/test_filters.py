"""Discovery-filter resolution tests using IGDB's real genre/platform names."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sources.igdb import IGDBClient  # noqa: E402

# IGDB's actual genre list, verbatim - note "Role-playing (RPG)", which is why
# a naive search for "rpg" found nothing.
GENRES = [
    {"id": 2, "name": "Point-and-click", "slug": "point-and-click"},
    {"id": 4, "name": "Fighting", "slug": "fighting"},
    {"id": 5, "name": "Shooter", "slug": "shooter"},
    {"id": 7, "name": "Music", "slug": "music"},
    {"id": 8, "name": "Platform", "slug": "platform"},
    {"id": 9, "name": "Puzzle", "slug": "puzzle"},
    {"id": 10, "name": "Racing", "slug": "racing"},
    {"id": 11, "name": "Real Time Strategy (RTS)", "slug": "real-time-strategy-rts"},
    {"id": 12, "name": "Role-playing (RPG)", "slug": "role-playing-rpg"},
    {"id": 13, "name": "Simulator", "slug": "simulator"},
    {"id": 14, "name": "Sport", "slug": "sport"},
    {"id": 15, "name": "Strategy", "slug": "strategy"},
    {"id": 16, "name": "Turn-based strategy (TBS)", "slug": "turn-based-strategy-tbs"},
    {"id": 24, "name": "Tactical", "slug": "tactical"},
    {"id": 25, "name": "Hack and slash/Beat 'em up", "slug": "hack-and-slash-beat-em-up"},
    {"id": 31, "name": "Adventure", "slug": "adventure"},
    {"id": 32, "name": "Indie", "slug": "indie"},
    {"id": 33, "name": "Arcade", "slug": "arcade"},
    {"id": 34, "name": "Visual Novel", "slug": "visual-novel"},
    {"id": 35, "name": "Card & Board Game", "slug": "card-and-board-game"},
    {"id": 36, "name": "MOBA", "slug": "moba"},
]

PLATFORMS = [
    {"id": 6, "name": "PC (Microsoft Windows)", "abbreviation": "PC", "slug": "win"},
    {"id": 48, "name": "PlayStation 4", "abbreviation": "PS4", "slug": "ps4"},
    {"id": 167, "name": "PlayStation 5", "abbreviation": "PS5", "slug": "ps5"},
    {"id": 49, "name": "Xbox One", "abbreviation": "XONE", "slug": "xone"},
    {"id": 169, "name": "Xbox Series X|S", "abbreviation": "Series X|S", "slug": "series-x-s"},
    {"id": 130, "name": "Nintendo Switch", "abbreviation": "Switch", "slug": "switch"},
    {"id": 508, "name": "Nintendo Switch 2", "abbreviation": "Switch 2", "slug": "switch-2"},
    {"id": 14, "name": "Mac", "abbreviation": "Mac", "slug": "mac"},
    {"id": 34, "name": "Android", "abbreviation": "Android", "slug": "android"},
]


class StubClient(IGDBClient):
    """IGDBClient with the network replaced by canned lookup tables."""

    def __init__(self):
        # Pass a dummy http client: the real one is never used here, and
        # constructing httpx.AsyncClient can fail in sandboxed environments.
        super().__init__("id", "secret", client=object())
        self.queries = []

    async def _query(self, endpoint, body, retry_on_auth=True):
        self.queries.append((endpoint, body))
        if endpoint == "genres":
            return GENRES
        if endpoint == "platforms":
            return PLATFORMS
        if endpoint == "companies":
            return [{"id": 5001, "name": "FromSoftware"}]
        return []


def resolve(kind, text):
    client = StubClient()
    return asyncio.run(client.resolve_filter(kind, text)), client


def test_the_reported_bug():
    """/watch genre:rpg — the exact command that failed."""
    result, _ = resolve("genre", "rpg")
    assert result == ("12", "Role-playing (RPG)"), result
    print("PASS: genre:rpg -> Role-playing (RPG)  [the reported bug]")


def test_genre_aliases_and_spellings():
    cases = {
        "rpg": 12, "RPG": 12, "jrpg": 12, "role playing": 12, "role-playing": 12,
        "fps": 5, "shooter": 5, "first person shooter": 5,
        "rts": 11, "tbs": 16, "turn based strategy": 16,
        "platformer": 8, "platform": 8,
        "sim": 13, "simulation": 13, "simulator": 13,
        "sports": 14, "sport": 14,
        "beat em up": 25, "brawler": 25,
        "vn": 34, "visual novel": 34,
        "card game": 35, "board game": 35,
        "moba": 36, "indie": 32, "puzzle": 9, "racing": 10,
        "adventure": 31, "fighting": 4, "tactical": 24,
    }
    for text, expected in cases.items():
        result, _ = resolve("genre", text)
        assert result is not None, f"{text!r} resolved to nothing"
        assert result[0] == str(expected), f"{text!r} -> {result}, expected id {expected}"
    print(f"PASS: {len(cases)} genre spellings and aliases all resolve")


def test_platform_aliases():
    cases = {
        "ps5": 167, "PS5": 167, "playstation 5": 167,
        "ps4": 48, "pc": 6, "windows": 6,
        "switch": 130, "nintendo switch": 130, "switch 2": 508,
        "xbox one": 49, "series x": 169, "xsx": 169,
        "mac": 14, "android": 34,
    }
    for text, expected in cases.items():
        result, _ = resolve("platform", text)
        assert result is not None, f"{text!r} resolved to nothing"
        assert result[0] == str(expected), f"{text!r} -> {result}, expected id {expected}"
    print(f"PASS: {len(cases)} platform spellings and aliases all resolve")


def test_typo_tolerance():
    assert resolve("platform", "playstaton 5")[0][0] == "167"
    assert resolve("genre", "advenutre")[0][0] == "31"
    print("PASS: typos still resolve via fuzzy fallback")


def test_nonsense_still_fails_cleanly():
    assert resolve("genre", "qqqzzzxyw")[0] is None
    assert resolve("genre", "")[0] is None
    assert resolve("bogus", "rpg")[0] is None
    print("PASS: genuine non-matches return None instead of a wrong answer")


def test_lookup_table_is_cached():
    client = StubClient()
    asyncio.run(client.resolve_filter("genre", "rpg"))
    asyncio.run(client.resolve_filter("genre", "fps"))
    asyncio.run(client.resolve_filter("genre", "moba"))
    genre_calls = [q for q in client.queries if q[0] == "genres"]
    assert len(genre_calls) == 1, f"expected 1 fetch, got {len(genre_calls)}"
    assert "search" not in genre_calls[0][1], "must not use search on /genres"
    print("PASS: genre table fetched once and reused; no search operator used")


def test_company_still_uses_server_search():
    result, client = resolve("company", "fromsoftware")
    assert result == ("5001", "FromSoftware")
    assert any(e == "companies" and "search" in b for e, b in client.queries)
    print("PASS: company lookup still uses IGDB server-side search")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nall filter tests passed")
