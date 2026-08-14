"""IGDB client (primary source).

Auth is Twitch app credentials: POST client_id/client_secret to Twitch, get an
app access token, send it with every IGDB call. Tokens last ~60 days; we cache
in memory and refresh on expiry or on a 401.

IGDB allows 4 requests/second per credential, so every call goes through a
rate limiter.
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timezone

import httpx

from .models import Game

logger = logging.getLogger(__name__)

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API_BASE = "https://api.igdb.com/v4"
COVER_TEMPLATE = "https://images.igdb.com/igdb/image/upload/t_cover_big/{}.jpg"

GAME_FIELDS = (
    "fields id,name,summary,url,first_release_date,"
    "platforms.abbreviation,platforms.name,"
    "cover.image_id,"
    "release_dates.date,release_dates.human,release_dates.category;"
)

# release_dates.category: 0 = exact YYYY-MM-DD. Everything else is a window
# (month, year, quarter, "TBD"), which we must NOT treat as a real date.
EXACT_CATEGORY = 0
_WINDOW_PATTERN = re.compile(r"\b(Q[1-4]|TBD|TBA)\b", re.IGNORECASE)

LOOKUP_LIMIT = 500

# Field lists differ per endpoint: IGDB rejects the ENTIRE query with a 400 if
# you ask for a field the endpoint doesn't have. /genres has no abbreviation
# or alternative_name; /platforms does.
LOOKUP_FIELDS = {
    "genres": "id,name,slug",
    "platforms": "id,name,slug,abbreviation,alternative_name",
}

# What people type -> what IGDB actually calls it. Only mappings that are
# unambiguously correct; anything else is left to substring/fuzzy matching.
GENRE_ALIASES = {
    "rpg": "role-playing",
    "jrpg": "role-playing",
    "arpg": "role-playing",
    "crpg": "role-playing",
    "roleplaying": "role-playing",
    "role playing": "role-playing",
    "fps": "shooter",
    "tps": "shooter",
    "first person shooter": "shooter",
    "rts": "real time strategy",
    "tbs": "turn-based strategy",
    "turn based strategy": "turn-based strategy",
    "platformer": "platform",
    "sim": "simulator",
    "sims": "simulator",
    "simulation": "simulator",
    "sports": "sport",
    "beat em up": "hack and slash",
    "brawler": "hack and slash",
    "vn": "visual novel",
    "board game": "card & board game",
    "card game": "card & board game",
    "point and click": "point-and-click",
}

PLATFORM_ALIASES = {
    "pc": "pc (microsoft windows)",
    "windows": "pc (microsoft windows)",
    "ps5": "playstation 5",
    "ps4": "playstation 4",
    "ps3": "playstation 3",
    "ps2": "playstation 2",
    "psx": "playstation",
    "ps1": "playstation",
    "xsx": "xbox series",
    "series x": "xbox series",
    "series s": "xbox series",
    "xbox series x": "xbox series",
    "xbone": "xbox one",
    "switch": "nintendo switch",
    "switch 2": "nintendo switch 2",
    "nsw": "nintendo switch",
    "mac": "mac",
    "macos": "mac",
}

_CANDIDATE_FIELDS = ("name", "slug", "abbreviation", "alternative_name")


def _row_labels(row):
    labels = []
    for field in _CANDIDATE_FIELDS:
        value = row.get(field)
        if value:
            labels.append(str(value).lower().replace("-", " ").strip())
    return labels


def _match_row(rows, query):
    """Best row for a user's text, tried strictest first."""
    query = query.lower().replace("-", " ").strip()
    if not query:
        return None

    scored = [(row, _row_labels(row)) for row in rows]

    for row, labels in scored:  # exact
        if query in labels:
            return row
    for row, labels in scored:  # prefix
        if any(label.startswith(query) for label in labels):
            return row
    for row, labels in scored:  # substring
        if any(query in label for label in labels):
            return row

    # Last resort: closest spelling, to forgive typos like "playstaton".
    import difflib

    flat = {label: row for row, labels in scored for label in labels}
    close = difflib.get_close_matches(query, list(flat), n=1, cutoff=0.75)
    return flat[close[0]] if close else None


class RateLimiter:
    """Spaces calls at most `rate` per second (IGDB allows 4/s)."""

    def __init__(self, rate=4):
        self._min_interval = 1.0 / max(rate, 1)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self):
        async with self._lock:
            gap = time.monotonic() - self._last
            if gap < self._min_interval:
                await asyncio.sleep(self._min_interval - gap)
            self._last = time.monotonic()


def _is_exact(release_date_row):
    category = release_date_row.get("category")
    if category is not None:
        return category == EXACT_CATEGORY
    # Older/newer payloads may omit category; fall back to reading the label.
    human = release_date_row.get("human") or ""
    return not _WINDOW_PATTERN.search(human) and bool(re.search(r"\d{1,2},?\s*\d{4}", human))


def _ts_to_date(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()


def to_game(raw):
    """Convert one IGDB game payload into a Game."""
    release_date = None
    human = "TBD"

    rows = [r for r in (raw.get("release_dates") or []) if r.get("date")]
    if rows:
        earliest = min(rows, key=lambda r: r["date"])
        human = earliest.get("human") or "TBD"
        if _is_exact(earliest):
            release_date = _ts_to_date(earliest["date"])
    elif raw.get("first_release_date"):
        release_date = _ts_to_date(raw["first_release_date"])
        human = release_date.strftime("%d %b %Y")

    platforms = []
    for platform in raw.get("platforms") or []:
        label = platform.get("abbreviation") or platform.get("name")
        if label:
            platforms.append(label)

    cover = ""
    image_id = (raw.get("cover") or {}).get("image_id")
    if image_id:
        cover = COVER_TEMPLATE.format(image_id)

    return Game(
        source="igdb",
        source_id=str(raw["id"]),
        name=raw.get("name") or "Unknown",
        release_date=release_date,
        release_human=human,
        platforms=platforms,
        url=raw.get("url") or "",
        cover_url=cover,
        summary=(raw.get("summary") or "")[:400],
    )


class IGDBClient:
    def __init__(self, client_id, client_secret, client=None, rate=4):
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self._http = client or httpx.AsyncClient(timeout=20)
        self._limiter = RateLimiter(rate)
        self._token = None
        self._token_expires = 0.0
        self._token_lock = asyncio.Lock()
        # Genres and platforms barely change; fetch each list once per process.
        self._lookup_cache = {}

    @property
    def enabled(self):
        return bool(self.client_id and self.client_secret)

    async def aclose(self):
        await self._http.aclose()

    # -- auth --------------------------------------------------------------
    async def _access_token(self, force=False):
        async with self._token_lock:
            if not force and self._token and time.time() < self._token_expires - 60:
                return self._token
            resp = await self._http.post(
                TOKEN_URL,
                params={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials",
                },
            )
            resp.raise_for_status()
            payload = resp.json()
            self._token = payload["access_token"]
            self._token_expires = time.time() + int(payload.get("expires_in", 3600))
            logger.info("IGDB token refreshed")
            return self._token

    async def _query(self, endpoint, body, retry_on_auth=True):
        if not self.enabled:
            return []
        token = await self._access_token()
        await self._limiter.wait()
        resp = await self._http.post(
            f"{API_BASE}/{endpoint}",
            headers={
                "Client-ID": self.client_id,
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            content=body,
        )
        if resp.status_code == 401 and retry_on_auth:
            logger.info("IGDB token rejected, refreshing once")
            await self._access_token(force=True)
            return await self._query(endpoint, body, retry_on_auth=False)
        if resp.status_code == 429:
            logger.warning("IGDB rate limited; backing off 1s")
            await asyncio.sleep(1.0)
            return await self._query(endpoint, body, retry_on_auth=False)
        resp.raise_for_status()
        return resp.json()

    # -- queries -----------------------------------------------------------
    async def search_games(self, name, limit=8):
        safe = name.replace('"', " ").strip()
        if not safe:
            return []
        body = f'search "{safe}"; {GAME_FIELDS} limit {int(limit)};'
        try:
            rows = await self._query("games", body)
        except httpx.HTTPError as exc:
            logger.warning("IGDB search failed for %r: %s", name, exc)
            return []
        return [to_game(row) for row in rows]

    async def games_by_ids(self, ids):
        """Fetch many games at once - the tracker's hot path."""
        ids = [str(int(i)) for i in ids]
        if not ids:
            return []
        games = []
        # IGDB caps a page at 500; chunk well under that to keep bodies small.
        for start in range(0, len(ids), 200):
            chunk = ids[start : start + 200]
            body = (
                f"where id = ({','.join(chunk)}); {GAME_FIELDS} limit {len(chunk)};"
            )
            try:
                rows = await self._query("games", body)
            except httpx.HTTPError as exc:
                logger.warning("IGDB batch fetch failed: %s", exc)
                continue
            games.extend(to_game(row) for row in rows)
        return games

    async def _lookup_table(self, endpoint):
        """All rows of a small reference endpoint, fetched once and cached.

        IGDB's `search` operator is not supported on /genres, and genre names
        are things like "Role-playing (RPG)" that a naive search would miss
        anyway. There are only ~23 genres and ~230 platforms, so pulling the
        whole list once and matching locally is both cheaper and far more
        forgiving.
        """
        if endpoint in self._lookup_cache:
            return self._lookup_cache[endpoint]
        fields = LOOKUP_FIELDS.get(endpoint, "id,name,slug")
        rows = []
        offset = 0
        while True:
            body = f"fields {fields}; limit {LOOKUP_LIMIT}; offset {offset};"
            try:
                page = await self._query(endpoint, body)
            except httpx.HTTPError as exc:
                logger.warning("IGDB %s lookup table failed: %s", endpoint, exc)
                break
            rows.extend(page)
            if len(page) < LOOKUP_LIMIT:
                break
            offset += LOOKUP_LIMIT
            if offset >= 2000:  # safety valve
                break
        if rows:
            self._lookup_cache[endpoint] = rows
            logger.info("IGDB %s table cached (%d rows)", endpoint, len(rows))
        return rows

    async def resolve_filter(self, kind, text):
        """Turn 'rpg' / 'ps5' / 'fromsoftware' into an IGDB id + label."""
        query = (text or "").strip().lower()
        if not query:
            return None

        if kind in ("genre", "platform"):
            endpoint = "genres" if kind == "genre" else "platforms"
            aliases = GENRE_ALIASES if kind == "genre" else PLATFORM_ALIASES
            rows = await self._lookup_table(endpoint)
            if not rows:
                return None
            match = _match_row(rows, aliases.get(query, query))
            if match is None and query in aliases:
                match = _match_row(rows, query)  # try the raw text too
            if match is None:
                return None
            return str(match["id"]), match.get("name") or text

        if kind == "company":
            # Hundreds of thousands of companies, so a server-side search is
            # the only sane option here - and /companies does support it.
            safe = text.replace('"', " ").strip()
            try:
                rows = await self._query(
                    "companies", f'search "{safe}"; fields id,name; limit 5;'
                )
            except httpx.HTTPError as exc:
                logger.warning("IGDB company lookup failed for %r: %s", text, exc)
                return None
            if not rows:
                return None
            return str(rows[0]["id"]), rows[0].get("name") or safe

        return None

    async def discover(self, kind, value_id, limit=15):
        """Upcoming games matching a discovery rule, newest listings first."""
        now = int(time.time())
        clause = {
            "genre": f"genres = ({value_id})",
            "platform": f"platforms = ({value_id})",
            "company": f"involved_companies.company = ({value_id})",
        }.get(kind)
        if clause is None:
            return []
        body = (
            f"where {clause} & first_release_date > {now}; "
            f"{GAME_FIELDS} sort first_release_date asc; limit {int(limit)};"
        )
        try:
            rows = await self._query("games", body)
        except httpx.HTTPError as exc:
            logger.warning("IGDB discovery failed (%s=%s): %s", kind, value_id, exc)
            return []
        return [to_game(row) for row in rows]
