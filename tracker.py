"""The scheduled jobs: release alerts, date changes, discovery, weekly digest.

Message-building functions here are deliberately pure (no network, no DB) so
they can be tested directly - see tests/test_tracker.py.
"""

import asyncio
import html
import logging
from datetime import date, timedelta

import config
import db
from sources.models import describe_change

logger = logging.getLogger(__name__)

# A game whose date passed more than this long ago is backfill, not news. It
# gets marked notified silently so the user isn't told that a game from 2019
# "is out now" the moment they track it.
STALE_RELEASE_DAYS = 7


# --------------------------------------------------------------------------
# Message formatting (pure)
# --------------------------------------------------------------------------
def _esc(value):
    return html.escape(str(value or ""))


def _link(name, url):
    if url:
        return f'<a href="{_esc(url)}">{_esc(name)}</a>'
    return f"<b>{_esc(name)}</b>"


def format_change_message(game, change):
    lines = [f"📅 <b>Date change</b> — {_link(game.name, game.url)}", _esc(change)]
    if game.platforms:
        lines.append(f"<i>{_esc(game.platform_label)}</i>")
    return "\n".join(lines)


def format_release_message(name, url, platforms):
    lines = [f"🎮 <b>Out now</b> — {_link(name, url)}"]
    if platforms:
        lines.append(f"<i>{_esc(platforms)}</i>")
    return "\n".join(lines)


def format_digest(rows, today, window_days):
    """Weekly lookahead. Returns None when there is nothing to say."""
    if not rows:
        return None
    end = today + timedelta(days=window_days)
    header = (
        f"🗓 <b>Next {window_days} days</b> "
        f"({today.strftime('%d %b')} – {end.strftime('%d %b')})"
    )
    lines = [header, ""]
    for row in rows:
        when = row["release_date"]
        days = (when - today).days
        if days == 0:
            suffix = "today"
        elif days == 1:
            suffix = "tomorrow"
        else:
            suffix = f"in {days} days"
        lines.append(
            f"• {_link(row['name'], row['url'])} — "
            f"{when.strftime('%a %d %b')} ({suffix})"
        )
    return "\n".join(lines)


def format_discovery(label, games):
    if not games:
        return None
    lines = [f"✨ <b>New for {_esc(label)}</b>", ""]
    for game in games:
        lines.append(f"• {_link(game.name, game.url)} — {_esc(game.release_human)}")
    lines.append("")
    lines.append("Use /track &lt;name&gt; to follow any of these.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------
class Notifier:
    """Sends messages, tolerates blocked users, and paces itself.

    Telegram allows roughly 30 messages/second overall; a small delay between
    sends keeps a big digest run well clear of that.
    """

    def __init__(self, bot, delay=None):
        self.bot = bot
        self.delay = config.SEND_DELAY_SECONDS if delay is None else delay
        self.sent = 0
        self.failed = 0

    async def send(self, chat_id, text):
        from telegram.error import Forbidden, RetryAfter, TelegramError

        try:
            await self.bot.send_message(
                chat_id,
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            self.sent += 1
        except Forbidden:
            # User blocked the bot or deleted the chat - stop writing to them.
            logger.info("chat %s blocked the bot; removing", chat_id)
            await db.forget_chat(chat_id)
            self.failed += 1
            return False
        except RetryAfter as exc:
            wait = getattr(exc, "retry_after", 5)
            logger.warning("flood control, sleeping %ss", wait)
            await asyncio.sleep(float(wait) + 0.5)
            return await self.send(chat_id, text)
        except TelegramError as exc:
            logger.warning("send to %s failed: %s", chat_id, exc)
            self.failed += 1
            return False
        if self.delay:
            await asyncio.sleep(self.delay)
        return True


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------
async def refresh_and_report_changes(catalog, notifier):
    """Re-fetch every tracked game and announce any date movement."""
    keys = await db.distinct_game_keys()
    if not keys:
        return 0
    live = await catalog.refresh(keys)
    changes = 0

    for key, game in live.items():
        rows = await db.subscribers_of(key)
        for row in rows:
            change = describe_change(
                row["release_human"],
                row["release_date"],
                game.release_human,
                game.release_date,
            )
            if change is None:
                continue
            await notifier.send(row["chat_id"], format_change_message(game, change))
            changes += 1
        # Snapshot last, so a crash mid-run re-reports rather than silently
        # swallowing the change on the next pass.
        await db.update_snapshot(key, game)

    logger.info("refresh: %d game(s), %d change notice(s)", len(live), changes)
    return changes


async def announce_releases(notifier, today=None):
    today = today or date.today()
    cutoff = today - timedelta(days=STALE_RELEASE_DAYS)
    rows = await db.due_releases(today)
    announced = 0

    for row in rows:
        if row["release_date"] < cutoff:
            # Old news - mark it so we never look at it again, but stay quiet.
            await db.mark_released_notified(row["chat_id"], row["game_key"])
            continue
        ok = await notifier.send(
            row["chat_id"],
            format_release_message(row["name"], row["url"], row["platforms"]),
        )
        if ok:
            await db.mark_released_notified(row["chat_id"], row["game_key"])
            announced += 1

    logger.info("releases: %d announced of %d due", announced, len(rows))
    return announced


async def run_discovery(catalog, notifier):
    """Surface newly listed upcoming games matching each chat's rules."""
    rules = await db.list_rules()
    if not rules:
        return 0

    # Cache per (kind, value_id): several chats often watch the same genre.
    cache = {}
    by_chat = {}
    for rule in rules:
        by_chat.setdefault(rule["chat_id"], []).append(rule)

    announced = 0
    for chat_id, chat_rules in by_chat.items():
        messages = 0
        for rule in chat_rules:
            if messages >= config.DISCOVERY_MAX_MESSAGES:
                break
            cache_key = (rule["kind"], rule["value_id"])
            if cache_key not in cache:
                cache[cache_key] = await catalog.discover(
                    rule["kind"], rule["value_id"], limit=config.DISCOVERY_PER_RULE
                )
            games = cache[cache_key]
            if not games:
                continue

            keys = [g.key for g in games]
            fresh_keys = set(await db.filter_unseen(chat_id, keys))
            if not fresh_keys:
                continue
            tracked = await db.already_tracked(chat_id, list(fresh_keys))
            picks = [g for g in games if g.key in fresh_keys and g.key not in tracked]
            text = format_discovery(rule["label"], picks[:10])
            if text is None:
                continue
            await notifier.send(chat_id, text)
            messages += 1
            announced += len(picks[:10])

    logger.info("discovery: %d new game(s) surfaced", announced)
    return announced


async def run_digest(notifier, today=None, force=False):
    today = today or date.today()
    if not force and today.weekday() != config.DIGEST_WEEKDAY:
        logger.info("digest: not the configured day, skipping")
        return 0
    end = today + timedelta(days=config.DIGEST_WINDOW_DAYS)
    sent = 0
    for chat_id in await db.all_chat_ids():
        rows = await db.upcoming_for_chat(chat_id, today, end)
        text = format_digest(rows, today, config.DIGEST_WINDOW_DAYS)
        if text is None:
            continue
        if await notifier.send(chat_id, text):
            sent += 1
    logger.info("digest: sent to %d chat(s)", sent)
    return sent


async def run_job(job, catalog, notifier, today=None):
    """Entry point used by the /tasks/run endpoint and the CLI."""
    today = today or date.today()
    summary = {}
    try:
        if job in ("daily", "all"):
            summary["changes"] = await refresh_and_report_changes(catalog, notifier)
            summary["released"] = await announce_releases(notifier, today)
            summary["discovered"] = await run_discovery(catalog, notifier)
        if job in ("weekly", "all"):
            summary["digest"] = await run_digest(notifier, today)
        if not summary:
            raise ValueError(f"unknown job: {job}")
    except Exception as exc:
        logger.exception("job %s failed", job)
        await db.record_job(job, "error", str(exc))
        raise
    await db.record_job(job, "ok", str(summary))
    return summary
