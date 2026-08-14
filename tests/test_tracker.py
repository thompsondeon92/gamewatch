"""Job logic tests with an in-memory fake database. No network, no Postgres."""

import asyncio
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import config  # noqa: E402
import db  # noqa: E402
import tracker  # noqa: E402
from sources.models import Game  # noqa: E402

TODAY = date(2026, 8, 13)


class FakeNotifier:
    def __init__(self):
        self.messages = []

    async def send(self, chat_id, text):
        self.messages.append((chat_id, text))
        return True


class FakeDB:
    """Stands in for the db module; only the calls tracker.py makes."""

    def __init__(self, subs=None, rules=None, seen=None, chats=None):
        self.subs = subs or []
        self.rules = rules or []
        self.seen = set(seen or ())
        self.chats = chats or []
        self.marked = []
        self.snapshots = {}
        self.jobs = []

    async def distinct_game_keys(self):
        return sorted({s["game_key"] for s in self.subs})

    async def subscribers_of(self, key):
        return [s for s in self.subs if s["game_key"] == key]

    async def update_snapshot(self, key, game):
        self.snapshots[key] = (game.release_date, game.release_human)
        for sub in self.subs:
            if sub["game_key"] == key:
                sub["release_date"] = game.release_date
                sub["release_human"] = game.release_human

    async def due_releases(self, today=None):
        return [
            s
            for s in self.subs
            if not s["released_notified"]
            and s["release_date"] is not None
            and s["release_date"] <= (today or TODAY)
        ]

    async def mark_released_notified(self, chat_id, key):
        self.marked.append((chat_id, key))
        for sub in self.subs:
            if sub["chat_id"] == chat_id and sub["game_key"] == key:
                sub["released_notified"] = True

    async def list_rules(self, chat_id=None):
        if chat_id is None:
            return self.rules
        return [r for r in self.rules if r["chat_id"] == chat_id]

    async def filter_unseen(self, chat_id, keys):
        fresh = [k for k in keys if (chat_id, k) not in self.seen]
        self.seen.update((chat_id, k) for k in fresh)
        return fresh

    async def already_tracked(self, chat_id, keys):
        tracked = {s["game_key"] for s in self.subs if s["chat_id"] == chat_id}
        return {k for k in keys if k in tracked}

    async def all_chat_ids(self):
        return self.chats

    async def upcoming_for_chat(self, chat_id, start, end):
        rows = [
            s
            for s in self.subs
            if s["chat_id"] == chat_id
            and s["release_date"] is not None
            and start <= s["release_date"] <= end
        ]
        return sorted(rows, key=lambda r: (r["release_date"], r["name"]))

    async def record_job(self, job, status, detail=""):
        self.jobs.append((job, status))

    async def forget_chat(self, chat_id):
        self.chats = [c for c in self.chats if c != chat_id]


class FakeCatalog:
    def __init__(self, live=None, discoveries=None):
        self.live = live or {}
        self.discoveries = discoveries or {}
        self.discover_calls = 0

    async def refresh(self, keys):
        return {k: v for k, v in self.live.items() if k in set(keys)}

    async def discover(self, kind, value_id, limit=15):
        self.discover_calls += 1
        return self.discoveries.get((kind, value_id), [])


def _sub(chat_id, key, name, release_date, human, notified=False):
    return {
        "chat_id": chat_id,
        "game_key": key,
        "name": name,
        "release_date": release_date,
        "release_human": human,
        "platforms": "PC",
        "url": f"https://example.com/{key}",
        "released_notified": notified,
    }


def _install(fake):
    for attr in dir(fake):
        if not attr.startswith("_"):
            value = getattr(fake, attr)
            if callable(value):
                setattr(db, attr, value)


# --------------------------------------------------------------------------
# Formatting (pure)
# --------------------------------------------------------------------------
def test_digest_windowing():
    rows = [
        _sub(1, "igdb:1", "Today Game", TODAY, "13 Aug 2026"),
        _sub(1, "igdb:2", "Tomorrow Game", TODAY + timedelta(days=1), "14 Aug 2026"),
        _sub(1, "igdb:3", "Later Game", TODAY + timedelta(days=20), "02 Sep 2026"),
    ]
    text = tracker.format_digest(rows, TODAY, 30)
    assert "today" in text and "tomorrow" in text and "in 20 days" in text
    assert tracker.format_digest([], TODAY, 30) is None, "empty digest must be silent"
    print("PASS: digest relative-day wording, and no message when empty")


def test_html_escaping():
    """Game titles contain & and < often enough to matter."""
    game = Game("igdb", "1", "Tom & Jerry <Chase>", None, "TBD", [], "https://x/?a=1&b=2")
    text = tracker.format_change_message(game, "window changed: A → B")
    assert "Tom &amp; Jerry &lt;Chase&gt;" in text
    assert "a=1&amp;b=2" in text
    print("PASS: titles and URLs are HTML-escaped")


def test_discovery_message_empty():
    assert tracker.format_discovery("RPG", []) is None
    print("PASS: discovery message suppressed when there is nothing new")


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------
def test_stale_release_is_marked_but_silent():
    """Tracking an old game must not fire an 'out now' alert."""
    fake = FakeDB(
        subs=[
            _sub(1, "igdb:old", "Ancient Game", date(2019, 1, 1), "01 Jan 2019"),
            _sub(1, "igdb:new", "Fresh Game", TODAY, "13 Aug 2026"),
        ]
    )
    _install(fake)
    notifier = FakeNotifier()
    count = asyncio.run(tracker.announce_releases(notifier, TODAY))
    assert count == 1, count
    assert len(notifier.messages) == 1
    assert "Fresh Game" in notifier.messages[0][1]
    assert (1, "igdb:old") in fake.marked, "stale game must still be marked, silently"
    print("PASS: stale releases marked without notifying; fresh release announced")


def test_change_detection_and_snapshot():
    fake = FakeDB(
        subs=[
            _sub(1, "igdb:1", "Delayed Game", date(2026, 9, 1), "01 Sep 2026"),
            _sub(2, "igdb:1", "Delayed Game", date(2026, 9, 1), "01 Sep 2026"),
            _sub(3, "igdb:2", "Steady Game", date(2026, 9, 1), "01 Sep 2026"),
        ]
    )
    _install(fake)
    catalog = FakeCatalog(
        live={
            "igdb:1": Game("igdb", "1", "Delayed Game", date(2027, 3, 1), "01 Mar 2027", ["PC"], ""),
            "igdb:2": Game("igdb", "2", "Steady Game", date(2026, 9, 1), "01 Sep 2026", ["PC"], ""),
        }
    )
    notifier = FakeNotifier()
    changes = asyncio.run(tracker.refresh_and_report_changes(catalog, notifier))
    assert changes == 2, "both subscribers of the delayed game get told"
    assert {m[0] for m in notifier.messages} == {1, 2}
    assert "delayed by 181 days" in notifier.messages[0][1]
    assert fake.snapshots["igdb:1"] == (date(2027, 3, 1), "01 Mar 2027")
    print("PASS: date change notifies every subscriber and updates the snapshot")


def test_no_change_no_message():
    fake = FakeDB(subs=[_sub(1, "igdb:1", "Same", date(2026, 9, 1), "01 Sep 2026")])
    _install(fake)
    catalog = FakeCatalog(
        live={"igdb:1": Game("igdb", "1", "Same", date(2026, 9, 1), "01 Sep 2026", [], "")}
    )
    notifier = FakeNotifier()
    assert asyncio.run(tracker.refresh_and_report_changes(catalog, notifier)) == 0
    assert notifier.messages == []
    print("PASS: unchanged games produce no noise")


def test_discovery_dedupes_and_skips_tracked():
    already = Game("igdb:x", "x", "Already Tracked", None, "TBD", [], "")
    already.source, already.source_id = "igdb", "x"
    fresh = Game("igdb", "y", "Brand New", None, "Q4 2026", [], "")
    fake = FakeDB(
        subs=[_sub(1, "igdb:x", "Already Tracked", None, "TBD")],
        rules=[{"chat_id": 1, "kind": "genre", "value_id": "12", "label": "RPG"}],
    )
    _install(fake)
    catalog = FakeCatalog(discoveries={("genre", "12"): [already, fresh]})
    notifier = FakeNotifier()

    asyncio.run(tracker.run_discovery(catalog, notifier))
    assert len(notifier.messages) == 1
    body = notifier.messages[0][1]
    assert "Brand New" in body
    assert "Already Tracked" not in body, "must not re-suggest a game you already follow"

    # Second run: everything has been seen, so nothing is sent again.
    notifier2 = FakeNotifier()
    asyncio.run(tracker.run_discovery(catalog, notifier2))
    assert notifier2.messages == [], "discovery must not repeat itself"
    print("PASS: discovery skips tracked games and never repeats a suggestion")


def test_digest_respects_weekday():
    fake = FakeDB(
        subs=[_sub(1, "igdb:1", "Soon", TODAY + timedelta(days=3), "16 Aug 2026")],
        chats=[1],
    )
    _install(fake)
    wednesday = date(2026, 8, 12)
    assert wednesday.weekday() == 2
    notifier = FakeNotifier()
    assert asyncio.run(tracker.run_digest(notifier, wednesday)) == 0
    assert notifier.messages == []

    monday = date(2026, 8, 10)
    assert monday.weekday() == config.DIGEST_WEEKDAY
    notifier2 = FakeNotifier()
    assert asyncio.run(tracker.run_digest(notifier2, monday)) == 1

    notifier3 = FakeNotifier()
    assert asyncio.run(tracker.run_digest(notifier3, wednesday, force=True)) == 1
    print("PASS: digest only fires on its weekday, unless forced")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nall tracker tests passed")
