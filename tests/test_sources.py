"""Parser and merge tests. No network, no database."""

import asyncio
import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sources import igdb, rawg  # noqa: E402
from sources.catalog import Catalog, split_key  # noqa: E402
from sources.models import Game, describe_change, merge  # noqa: E402

NOV_12 = int(datetime(2026, 11, 12, tzinfo=timezone.utc).timestamp())


def test_igdb_exact_date():
    game = igdb.to_game(
        {
            "id": 1020,
            "name": "Grand Theft Auto VI",
            "url": "https://igdb.com/games/gta6",
            "release_dates": [
                {"date": NOV_12, "human": "Nov 12, 2026", "category": 0},
                {"date": NOV_12 + 86400 * 90, "human": "Feb 10, 2027", "category": 0},
            ],
            "platforms": [{"abbreviation": "PS5"}, {"name": "Xbox Series X|S"}],
            "cover": {"image_id": "abc123"},
        }
    )
    assert game.key == "igdb:1020"
    assert game.release_date == date(2026, 11, 12), game.release_date
    assert game.release_human == "Nov 12, 2026"
    assert game.platforms == ["PS5", "Xbox Series X|S"]
    assert game.cover_url.endswith("abc123.jpg")
    print("PASS: IGDB exact date, earliest of several, platform + cover mapping")


def test_igdb_window_is_not_a_date():
    """A 'Q3 2026' window must NOT become a calendar date."""
    game = igdb.to_game(
        {
            "id": 7,
            "name": "Windowed Game",
            "release_dates": [{"date": NOV_12, "human": "Q3 2026", "category": 3}],
        }
    )
    assert game.release_date is None, game.release_date
    assert game.release_human == "Q3 2026"
    print("PASS: IGDB release window kept as a label, not a fake exact date")


def test_igdb_missing_category_falls_back_to_label():
    windowed = igdb.to_game(
        {"id": 8, "name": "X", "release_dates": [{"date": NOV_12, "human": "Q1 2027"}]}
    )
    exact = igdb.to_game(
        {"id": 9, "name": "Y", "release_dates": [{"date": NOV_12, "human": "Nov 12, 2026"}]}
    )
    assert windowed.release_date is None
    assert exact.release_date == date(2026, 11, 12)
    print("PASS: IGDB payload without `category` still distinguishes window vs date")


def test_igdb_no_release_dates_uses_first_release_date():
    game = igdb.to_game({"id": 10, "name": "Z", "first_release_date": NOV_12})
    assert game.release_date == date(2026, 11, 12)
    print("PASS: IGDB falls back to first_release_date")


def test_rawg_tba():
    tba = rawg.to_game({"id": 5, "name": "Someday", "released": "2026-11-12", "tba": True})
    assert tba.release_date is None and tba.release_human == "TBA"
    dated = rawg.to_game(
        {
            "id": 6,
            "name": "Dated",
            "released": "2026-11-12",
            "slug": "dated",
            "platforms": [{"platform": {"name": "PC"}}],
        }
    )
    assert dated.release_date == date(2026, 11, 12)
    assert dated.url == "https://rawg.io/games/dated"
    assert dated.platforms == ["PC"]
    assert rawg.to_game({"id": 7, "name": "Bad", "released": "not-a-date"}).release_date is None
    print("PASS: RAWG tba flag, date parsing, malformed date, platform mapping")


def test_merge_keeps_primary_identity():
    primary = Game("igdb", "1", "Game", None, "TBD", [], "")
    secondary = Game("rawg", "2", "Game", date(2026, 5, 1), "01 May 2026", ["PC"], "u")
    merged = merge(primary, secondary)
    assert merged.key == "igdb:1", "must keep IGDB identity so subscriptions stay stable"
    assert merged.release_date == date(2026, 5, 1)
    assert merged.platforms == ["PC"]

    # A primary that already has a date must not be overwritten.
    strong = Game("igdb", "1", "Game", date(2026, 1, 1), "01 Jan 2026", ["PS5"], "")
    merged2 = merge(strong, secondary)
    assert merged2.release_date == date(2026, 1, 1)
    assert merged2.platforms == ["PS5"]
    print("PASS: merge fills gaps only, never overwrites primary or changes identity")


def test_describe_change():
    d1, d2 = date(2026, 5, 1), date(2026, 9, 1)
    assert describe_change("01 May", d1, "01 May", d1) is None
    assert "delayed by 123 days" in describe_change("01 May", d1, "01 Sep", d2)
    assert "moved up by 123 days" in describe_change("01 Sep", d2, "01 May", d1)
    assert "now has a date" in describe_change("Q3 2026", None, "01 Sep 2026", d2)
    assert "date pulled" in describe_change("01 Sep 2026", d2, "TBD", None)
    assert "window changed" in describe_change("Q3 2026", None, "Q4 2026", None)
    assert describe_change("TBD", None, "TBD", None) is None
    print("PASS: describe_change covers delay, pull-in, window->date, date->window, no-op")


def test_split_key():
    assert split_key("igdb:1020") == ("igdb", "1020")
    assert split_key("rawg:55") == ("rawg", "55")
    print("PASS: key splitting")


class FakeIGDB:
    enabled = True

    def __init__(self, results=None, by_id=None):
        self.results = results or []
        self.by_id = by_id or {}
        self.searched = []

    async def search_games(self, name, limit=8):
        self.searched.append(name)
        return list(self.results)

    async def games_by_ids(self, ids):
        return [self.by_id[i] for i in ids if i in self.by_id]

    async def aclose(self):
        pass


class FakeRAWG:
    enabled = True

    def __init__(self, results=None, best=None):
        self.results = results or []
        self.best = best
        self.searched = []

    async def search_games(self, name, limit=8):
        self.searched.append(name)
        return list(self.results)

    async def best_match(self, name):
        return self.best

    async def game_by_id(self, rawg_id):
        return None

    async def aclose(self):
        pass


def test_catalog_falls_back_to_rawg():
    fallback = Game("rawg", "9", "Only On RAWG", date(2026, 3, 3), "03 Mar 2026", [], "")
    catalog = Catalog(FakeIGDB(results=[]), FakeRAWG(results=[fallback]))
    found = asyncio.run(catalog.search("obscure"))
    assert len(found) == 1 and found[0].source == "rawg"

    igdb_hit = Game("igdb", "1", "Big Game", date(2026, 1, 1), "01 Jan 2026", ["PC"], "")
    rawg_client = FakeRAWG(results=[fallback])
    catalog2 = Catalog(FakeIGDB(results=[igdb_hit]), rawg_client)
    found2 = asyncio.run(catalog2.search("big game"))
    assert found2[0].source == "igdb"
    assert rawg_client.searched == [], "RAWG must not be called when IGDB answers"
    print("PASS: catalog uses RAWG only when IGDB returns nothing")


def test_catalog_enrich_rejects_mismatched_name():
    """RAWG search is fuzzy - a sequel's date must not leak into the original."""
    thin = Game("igdb", "1", "Silksong", None, "TBD", [], "")
    wrong = Game("rawg", "2", "Silksong 2", date(2030, 1, 1), "01 Jan 2030", ["PC"], "")
    catalog = Catalog(FakeIGDB(), FakeRAWG(best=wrong))
    result = asyncio.run(catalog.enrich(thin))
    assert result.release_date is None, "must not inherit a different game's date"
    assert result.platforms == ["PC"], "non-date fields are still safe to borrow"
    print("PASS: enrich refuses a date from a fuzzy name mismatch")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nall source tests passed")
