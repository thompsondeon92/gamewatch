"""Unified game model shared by the IGDB and RAWG clients."""

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class Game:
    """One game, normalised across sources.

    `key` is the stable identity used everywhere else ("igdb:1020"). It embeds
    the source so IGDB and RAWG IDs can never collide.

    `release_date` is an exact calendar date and may be None even when the game
    has a known release *window* - a game slated for "Q3 2026" has no date but
    does have `release_human`. Keeping both is the whole point: users want to
    hear about "Q3 2026 -> 12 Nov 2026" just as much as an outright delay.
    """

    source: str
    source_id: str
    name: str
    release_date: Optional[date] = None
    release_human: str = "TBD"
    platforms: list = field(default_factory=list)
    url: str = ""
    cover_url: str = ""
    summary: str = ""

    @property
    def key(self):
        return f"{self.source}:{self.source_id}"

    @property
    def platform_label(self):
        return ", ".join(self.platforms[:6]) if self.platforms else "platform TBD"

    def display(self):
        return f"{self.name} — {self.release_human} ({self.platform_label})"


def merge(primary: Optional[Game], secondary: Optional[Game]) -> Optional[Game]:
    """Fill gaps in `primary` from `secondary` without changing its identity.

    IGDB is the primary source; RAWG only ever contributes fields IGDB left
    empty. The merged game keeps IGDB's key so subscriptions stay stable even
    if RAWG later goes away.
    """
    if primary is None:
        return secondary
    if secondary is None:
        return primary

    if primary.release_date is None and secondary.release_date is not None:
        primary.release_date = secondary.release_date
        # Only inherit the human label if it actually adds information.
        if primary.release_human in ("", "TBD"):
            primary.release_human = secondary.release_human
    if not primary.platforms:
        primary.platforms = secondary.platforms
    if not primary.cover_url:
        primary.cover_url = secondary.cover_url
    if not primary.summary:
        primary.summary = secondary.summary
    if not primary.url:
        primary.url = secondary.url
    return primary


def describe_change(old_human, old_date, new_human, new_date):
    """Human sentence for a release-date change, or None if nothing moved.

    Returns None for cosmetic differences so users don't get pinged when a
    source merely reformats its own label.
    """
    if old_date == new_date and (old_human or "") == (new_human or ""):
        return None

    # A real calendar move is the interesting case.
    if old_date and new_date and old_date != new_date:
        direction = "delayed" if new_date > old_date else "moved up"
        delta = abs((new_date - old_date).days)
        unit = "day" if delta == 1 else "days"
        return f"{direction} by {delta} {unit}: {old_human} → {new_human}"

    if old_date is None and new_date is not None:
        return f"now has a date: {old_human} → {new_human}"

    if old_date is not None and new_date is None:
        return f"date pulled: {old_human} → {new_human}"

    if (old_human or "") != (new_human or ""):
        return f"window changed: {old_human} → {new_human}"

    return None
