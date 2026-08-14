"""Postgres storage (Supabase / Neon friendly).

Both providers front Postgres with a connection pooler. In transaction pooling
mode server-side prepared statements break, so the pool is created with
statement_cache_size=0 - without that you get intermittent
"prepared statement _pg_N already exists" errors that are miserable to debug.
"""

import logging
import os
from datetime import date

import asyncpg

logger = logging.getLogger(__name__)

_pool = None

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


async def init(dsn, min_size=1, max_size=4):
    """Create the pool and apply the schema. Safe to call repeatedly."""
    global _pool
    if _pool is not None:
        return _pool
    _pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=min_size,
        max_size=max_size,
        statement_cache_size=0,  # required behind Supabase/Neon poolers
        command_timeout=30,
    )
    with open(SCHEMA_PATH, "r", encoding="utf-8") as handle:
        schema = handle.read()
    async with _pool.acquire() as conn:
        await conn.execute(schema)
    logger.info("database ready")
    return _pool


async def close():
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool():
    if _pool is None:
        raise RuntimeError("db.init() has not been called")
    return _pool


# --------------------------------------------------------------------------
# Chats
# --------------------------------------------------------------------------
async def ensure_chat(chat_id, title=None):
    async with pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO chats (chat_id, title) VALUES ($1, $2)
            ON CONFLICT (chat_id) DO UPDATE SET title = COALESCE($2, chats.title)
            """,
            chat_id,
            title,
        )


async def all_chat_ids():
    async with pool().acquire() as conn:
        rows = await conn.fetch("SELECT chat_id FROM chats")
    return [r["chat_id"] for r in rows]


async def forget_chat(chat_id):
    """Used when Telegram tells us the user blocked the bot."""
    async with pool().acquire() as conn:
        await conn.execute("DELETE FROM chats WHERE chat_id = $1", chat_id)


# --------------------------------------------------------------------------
# Subscriptions
# --------------------------------------------------------------------------
async def add_subscription(chat_id, game):
    async with pool().acquire() as conn:
        result = await conn.execute(
            """
            INSERT INTO subscriptions
                (chat_id, game_key, name, release_date, release_human, platforms, url)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (chat_id, game_key) DO NOTHING
            """,
            chat_id,
            game.key,
            game.name,
            game.release_date,
            game.release_human,
            ", ".join(game.platforms),
            game.url,
        )
    return result.endswith("1")  # False means it was already tracked


async def remove_subscription(chat_id, game_key):
    async with pool().acquire() as conn:
        result = await conn.execute(
            "DELETE FROM subscriptions WHERE chat_id = $1 AND game_key = $2",
            chat_id,
            game_key,
        )
    return result.endswith("1")


async def list_subscriptions(chat_id):
    async with pool().acquire() as conn:
        return await conn.fetch(
            """
            SELECT * FROM subscriptions WHERE chat_id = $1
            ORDER BY release_date IS NULL, release_date, name
            """,
            chat_id,
        )


async def distinct_game_keys():
    async with pool().acquire() as conn:
        rows = await conn.fetch("SELECT DISTINCT game_key FROM subscriptions")
    return [r["game_key"] for r in rows]


async def subscribers_of(game_key):
    async with pool().acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM subscriptions WHERE game_key = $1", game_key
        )


async def update_snapshot(game_key, game):
    """Store the new state for every subscriber of this game."""
    async with pool().acquire() as conn:
        await conn.execute(
            """
            UPDATE subscriptions
               SET name = $2, release_date = $3, release_human = $4,
                   platforms = $5, url = $6
             WHERE game_key = $1
            """,
            game_key,
            game.name,
            game.release_date,
            game.release_human,
            ", ".join(game.platforms),
            game.url,
        )


async def mark_released_notified(chat_id, game_key):
    async with pool().acquire() as conn:
        await conn.execute(
            """
            UPDATE subscriptions SET released_notified = TRUE
             WHERE chat_id = $1 AND game_key = $2
            """,
            chat_id,
            game_key,
        )


async def due_releases(today=None):
    """Tracked games whose date has arrived and that we haven't announced."""
    today = today or date.today()
    async with pool().acquire() as conn:
        return await conn.fetch(
            """
            SELECT * FROM subscriptions
             WHERE released_notified = FALSE
               AND release_date IS NOT NULL
               AND release_date <= $1
            """,
            today,
        )


async def upcoming_for_chat(chat_id, start, end):
    async with pool().acquire() as conn:
        return await conn.fetch(
            """
            SELECT * FROM subscriptions
             WHERE chat_id = $1
               AND release_date IS NOT NULL
               AND release_date BETWEEN $2 AND $3
             ORDER BY release_date, name
            """,
            chat_id,
            start,
            end,
        )


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------
async def add_rule(chat_id, kind, value_id, label):
    async with pool().acquire() as conn:
        result = await conn.execute(
            """
            INSERT INTO discovery_rules (chat_id, kind, value_id, label)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (chat_id, kind, value_id) DO NOTHING
            """,
            chat_id,
            kind,
            value_id,
            label,
        )
    return result.endswith("1")


async def remove_rule(chat_id, kind, value_id):
    async with pool().acquire() as conn:
        result = await conn.execute(
            """
            DELETE FROM discovery_rules
             WHERE chat_id = $1 AND kind = $2 AND value_id = $3
            """,
            chat_id,
            kind,
            value_id,
        )
    return result.endswith("1")


async def list_rules(chat_id=None):
    async with pool().acquire() as conn:
        if chat_id is None:
            return await conn.fetch("SELECT * FROM discovery_rules")
        return await conn.fetch(
            "SELECT * FROM discovery_rules WHERE chat_id = $1 ORDER BY kind, label",
            chat_id,
        )


async def filter_unseen(chat_id, game_keys):
    """Return the subset this chat has never been shown, and record them."""
    if not game_keys:
        return []
    async with pool().acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                SELECT game_key FROM discovery_seen
                 WHERE chat_id = $1 AND game_key = ANY($2::text[])
                """,
                chat_id,
                list(game_keys),
            )
            already = {r["game_key"] for r in rows}
            fresh = [k for k in game_keys if k not in already]
            if fresh:
                await conn.executemany(
                    """
                    INSERT INTO discovery_seen (chat_id, game_key) VALUES ($1, $2)
                    ON CONFLICT DO NOTHING
                    """,
                    [(chat_id, key) for key in fresh],
                )
    return fresh


async def already_tracked(chat_id, game_keys):
    if not game_keys:
        return set()
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT game_key FROM subscriptions
             WHERE chat_id = $1 AND game_key = ANY($2::text[])
            """,
            chat_id,
            list(game_keys),
        )
    return {r["game_key"] for r in rows}


# --------------------------------------------------------------------------
# Job bookkeeping
# --------------------------------------------------------------------------
async def record_job(job, status, detail=""):
    async with pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO job_runs (job, last_run_at, last_status, detail)
            VALUES ($1, now(), $2, $3)
            ON CONFLICT (job) DO UPDATE
               SET last_run_at = now(), last_status = $2, detail = $3
            """,
            job,
            status,
            detail[:500],
        )


async def job_status():
    async with pool().acquire() as conn:
        return await conn.fetch("SELECT * FROM job_runs ORDER BY job")
