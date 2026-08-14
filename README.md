# GameWatch

A Telegram bot that tracks video game release dates. It tells you when a game
you follow launches, when its date moves, and once a week what's coming in the
next 30 days. It can also surface newly announced games in genres, on platforms,
or from studios you care about.

Runs entirely on free tiers.

---

## Commands

| Command | What it does |
|---|---|
| `/track <game>` | Follow a game (inline picker when the search is ambiguous) |
| `/untrack` | Stop following one |
| `/list` | Everything you follow |
| `/upcoming [days]` | What's coming, default 30 days |
| `/watch genre:rpg` | Hear about newly announced RPGs |
| `/watch platform:ps5` | …or anything coming to a platform |
| `/watch studio:fromsoftware` | …or from a studio |
| `/rules` · `/unwatch` | Manage discovery rules |
| `/digest` | Send this week's lookahead right now |
| `/status` | Which sources are live and when jobs last ran |

---

## How it works

**Two sources.** IGDB is primary — it has proper per-platform release records
and distinguishes an exact date from a window like "Q3 2026", which matters
enormously for a tracker. RAWG is the fallback: it answers searches IGDB misses
and fills gaps in IGDB records. Every game gets a stable key like `igdb:1020`,
so subscriptions survive even if one source goes away.

**Change detection compares against a snapshot.** Each subscription stores the
release date it was last shown. The daily job re-fetches every tracked game and
diffs against that snapshot, which is what lets it say "delayed by 181 days:
01 Sep 2026 → 01 Mar 2027" rather than just showing you a new date.

**One port serves everything.** A Starlette app owns the HTTP server; the
Telegram webhook and the private `/tasks/run` endpoint share it. PTB runs with
`updater=None` and is fed through its update queue.

---

## Setup

### 1. Credentials

| What | Where | Notes |
|---|---|---|
| Telegram token | [@BotFather](https://t.me/BotFather) | |
| Twitch Client ID + Secret | [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps) | This is how you authenticate to IGDB. Free for non-commercial use, 4 req/sec |
| RAWG key | [rawg.io/apidocs](https://rawg.io/apidocs) | Optional. 20,000 requests/month. **Requires attribution** — already in `/help` |
| Postgres URL | [Supabase](https://supabase.com) or [Neon](https://neon.tech) | See the warning below |

> **Don't use Render's free Postgres.** It expires 30 days after creation, with
> a 14-day grace period, then the data is deleted. Supabase and Neon free tiers
> don't expire. On Supabase, use the **connection pooling** URI.

### 2. Local run

```sh
cp .env.example .env      # fill in the values
pip install -r requirements.txt
python bot.py             # long polling, no HTTP server
```

Run a scheduled job by hand:

```sh
python bot.py --job daily     # releases, date changes, discovery
python bot.py --job weekly    # the digest
```

The schema is created automatically on first connect — no migration step.

### 3. Deploy to Render

Push the repo, create a Blueprint from `render.yaml`, and set the secrets it
prompts for. Generate the cron token with:

```sh
openssl rand -hex 32
```

`USE_WEBHOOK=true` is what makes this work as a web service: a long-polling bot
never binds a port and its deploy fails Render's port scan with
*"no open ports detected"*.

### 4. Wire up the scheduler

Render's cron jobs cost $1/mo minimum and background workers around $7/mo, so
`.github/workflows/scheduled-checks.yml` uses GitHub Actions instead — free on
public repos. It wakes the sleeping instance via `/healthz`, then calls
`/tasks/run`.

Add two repository secrets under **Settings → Secrets and variables → Actions**:

| Secret | Value |
|---|---|
| `GAMEWATCH_URL` | `https://your-service.onrender.com` (no trailing slash) |
| `GAMEWATCH_CRON_TOKEN` | the same string as `CRON_TOKEN` on Render |

Trigger it manually once from the Actions tab to confirm the wiring.

---

## Free-tier realities

- **The service sleeps after 15 minutes idle.** Telegram retries webhook
  deliveries, so messages aren't lost, but the first command after a quiet spell
  takes 30–60 seconds while the instance wakes. The scheduler pings `/healthz`
  first for exactly this reason.
- **GitHub's scheduler is best-effort** and can run late under load. It also
  disables workflows on repos with no activity for 60 days. If you need
  punctual delivery, a $1/mo Render cron job pointed at the same endpoint is the
  clean upgrade — no code changes.
- **IGDB allows 4 requests/second.** The client rate-limits itself, and the
  daily refresh batches up to 200 games per call, so a few hundred tracked games
  is only a handful of requests.

---

## Security notes

- `/tasks/run` and `/tasks/status` require the cron token, compared with
  `hmac.compare_digest` so the endpoint can't be probed a byte at a time. Prefer
  the `X-Cron-Token` header over the `?token=` query param — query strings leak
  into access logs.
- The Telegram webhook path is a SHA-256 of the bot token, and the request is
  additionally verified against `X-Telegram-Bot-Api-Secret-Token`.
- Job runs are serialized by a lock, so a retried or duplicated cron call can't
  double-send.
- All game titles and URLs are HTML-escaped before being sent.

---

## Tests

```sh
python tests/test_sources.py
python tests/test_tracker.py
```

Both pass with no network and no database. They cover:

- IGDB exact dates vs. windows — a `"Q3 2026"` label must never become a fake
  calendar date, including when the payload omits `category`
- RAWG `tba` handling and malformed dates
- Merge keeping IGDB's identity and never overwriting a known date
- Every branch of the change description: delay, pull-in, window→date,
  date→window, and no-op
- Catalog calling RAWG *only* when IGDB returns nothing
- Enrichment refusing a date from a fuzzy name match (so "Silksong 2" can't set
  the date for "Silksong")
- Tracking an old game marks it silently instead of announcing "out now"
- A date change notifying every subscriber and updating the snapshot
- Discovery skipping games you already track and never repeating a suggestion
- The digest firing only on its configured weekday unless forced
- HTML escaping of `&` and `<` in titles and URLs

## Not verified

The IGDB and RAWG clients are tested against synthetic payloads, not live
services — I had no API keys to call them with. The request shapes follow the
published docs, but the first live run is the real test. Start with:

```sh
python bot.py --job daily
```

and watch for `IGDB token refreshed`. If IGDB ever renames `release_dates.category`,
`_is_exact()` in `sources/igdb.py` falls back to reading the human label, so a
schema change degrades to "treat it as a window" rather than crashing.

---

*Game data from [IGDB](https://www.igdb.com). Game data partly from
[RAWG](https://rawg.io).*
