"""GameWatch — a Telegram bot that tracks video game release dates.

Serving model
------------
In webhook mode a Starlette app owns the HTTP server so that ONE port serves
both the Telegram webhook and the private /tasks/run endpoint the scheduler
calls. PTB runs with updater=None and receives updates through its update
queue - the pattern from PTB's own custom-webhook example.

Run `python bot.py --job daily` to execute a scheduled job once and exit.
"""

import argparse
import asyncio
import hashlib
import hmac
import logging
from datetime import date, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import config
import db
import tracker
from sources.catalog import Catalog
from sources.igdb import IGDBClient
from sources.rawg import RAWGClient
from sources.rawg import ATTRIBUTION as RAWG_ATTRIBUTION

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)
for noisy in ("httpx", "httpcore", "asyncpg"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

RULE_KINDS = {
    "genre": "genre",
    "genres": "genre",
    "platform": "platform",
    "studio": "company",
    "company": "company",
    "publisher": "company",
    "developer": "company",
}

HELP = (
    "<b>GameWatch</b> — never miss a release date.\n\n"
    "<b>Tracking</b>\n"
    "/track &lt;game&gt; — follow a game\n"
    "/untrack — stop following one\n"
    "/list — everything you follow\n"
    "/upcoming [days] — what's coming (default 30)\n\n"
    "<b>Discovery</b>\n"
    "/watch genre:rpg — hear about new RPGs\n"
    "/watch platform:ps5\n"
    "/watch studio:fromsoftware\n"
    "/rules — your discovery rules\n"
    "/unwatch — remove one\n\n"
    "<b>Other</b>\n"
    "/digest — send this week's lookahead now\n"
    "/status — source and job health\n\n"
    "You'll be messaged when a tracked game launches, when its date moves, "
    "and once a week with a 30-day lookahead.\n\n"
    f"<i>Data from IGDB. {RAWG_ATTRIBUTION}</i>"
)


def _catalog():
    return Catalog(
        IGDBClient(config.TWITCH_CLIENT_ID, config.TWITCH_CLIENT_SECRET),
        RAWGClient(config.RAWG_API_KEY),
    )


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await db.ensure_chat(chat.id, chat.title or chat.full_name)
    await update.message.reply_text(HELP, parse_mode="HTML")


async def cmd_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await db.ensure_chat(chat.id, chat.title or chat.full_name)

    if not context.args:
        await update.message.reply_text("Usage: /track <game name>")
        return

    existing = await db.list_subscriptions(chat.id)
    if len(existing) >= config.MAX_TRACKED_PER_CHAT:
        await update.message.reply_text(
            f"You're already tracking {len(existing)} games "
            f"(limit {config.MAX_TRACKED_PER_CHAT}). Use /untrack to make room."
        )
        return

    name = " ".join(context.args)
    catalog = context.bot_data["catalog"]
    results = await catalog.search(name, limit=8)
    if not results:
        await update.message.reply_text(f'No game found for "{name}".')
        return

    context.user_data["track_candidates"] = {g.key: g for g in results}
    if len(results) == 1:
        await _subscribe(update.effective_chat.id, context, results[0], update.message.reply_text)
        return

    buttons = [
        [InlineKeyboardButton(g.display()[:60], callback_data=f"tr:{g.key}")]
        for g in results[:8]
    ]
    await update.message.reply_text(
        "Which one?", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def _subscribe(chat_id, context, game, reply):
    catalog = context.bot_data["catalog"]
    game = await catalog.enrich(game)
    added = await db.add_subscription(chat_id, game)
    if not added:
        await reply(f"Already tracking <b>{game.name}</b>.", parse_mode="HTML")
        return
    when = game.release_human
    extra = ""
    if game.release_date:
        days = (game.release_date - date.today()).days
        if days > 0:
            extra = f" — {days} days away"
        elif days == 0:
            extra = " — that's today"
    await reply(
        f"✅ Tracking <b>{game.name}</b>\n{when}{extra}\n<i>{game.platform_label}</i>",
        parse_mode="HTML",
    )


async def cb_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data.split(":", 1)[1]
    game = (context.user_data.get("track_candidates") or {}).get(key)
    if game is None:
        await query.edit_message_text("That search expired — run /track again.")
        return
    await query.edit_message_text(f"Adding {game.name}…")
    await _subscribe(query.message.chat_id, context, game, query.edit_message_text)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await db.list_subscriptions(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("You're not tracking anything yet. Try /track Hollow Knight Silksong")
        return
    lines = [f"<b>Tracking {len(rows)} game(s)</b>", ""]
    for row in rows:
        marker = "✅" if row["released_notified"] else "•"
        lines.append(f"{marker} {tracker._link(row['name'], row['url'])} — {tracker._esc(row['release_human'])}")
    await update.message.reply_text(
        "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True
    )


async def cmd_untrack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await db.list_subscriptions(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("Nothing to untrack.")
        return
    buttons = [
        [InlineKeyboardButton(row["name"][:60], callback_data=f"un:{row['game_key']}")]
        for row in rows[:20]
    ]
    await update.message.reply_text(
        "Remove which?", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def cb_untrack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data.split(":", 1)[1]
    removed = await db.remove_subscription(query.message.chat_id, key)
    await query.edit_message_text("Removed." if removed else "That one wasn't tracked.")


async def cmd_upcoming(update: Update, context: ContextTypes.DEFAULT_TYPE):
    days = config.DIGEST_WINDOW_DAYS
    if context.args:
        try:
            days = max(1, min(365, int(context.args[0])))
        except ValueError:
            await update.message.reply_text("Usage: /upcoming [days]")
            return
    today = date.today()
    rows = await db.upcoming_for_chat(update.effective_chat.id, today, today + timedelta(days=days))
    text = tracker.format_digest(rows, today, days)
    await update.message.reply_text(
        text or f"Nothing dated in the next {days} days.",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await db.ensure_chat(chat.id, chat.title or chat.full_name)

    raw = " ".join(context.args)
    if ":" not in raw:
        await update.message.reply_text(
            "Usage: /watch genre:rpg — or platform:ps5, studio:fromsoftware"
        )
        return
    prefix, _, value = raw.partition(":")
    kind = RULE_KINDS.get(prefix.strip().lower())
    if kind is None or not value.strip():
        await update.message.reply_text(
            "Unknown filter. Use genre:, platform: or studio:"
        )
        return

    rules = await db.list_rules(chat.id)
    if len(rules) >= config.MAX_RULES_PER_CHAT:
        await update.message.reply_text(
            f"You already have {len(rules)} rules (limit {config.MAX_RULES_PER_CHAT})."
        )
        return

    resolved = await context.bot_data["catalog"].resolve_filter(kind, value.strip())
    if resolved is None:
        await update.message.reply_text(f'Couldn\'t find a {kind} matching "{value.strip()}".')
        return
    value_id, label = resolved
    added = await db.add_rule(chat.id, kind, value_id, label)
    await update.message.reply_text(
        f"👀 Watching {kind}: <b>{tracker._esc(label)}</b>" if added
        else f"Already watching {kind}: {tracker._esc(label)}",
        parse_mode="HTML",
    )


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rules = await db.list_rules(update.effective_chat.id)
    if not rules:
        await update.message.reply_text("No discovery rules. Try /watch genre:rpg")
        return
    lines = ["<b>Discovery rules</b>", ""]
    for rule in rules:
        lines.append(f"• {rule['kind']}: {tracker._esc(rule['label'])}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rules = await db.list_rules(update.effective_chat.id)
    if not rules:
        await update.message.reply_text("No rules to remove.")
        return
    buttons = [
        [
            InlineKeyboardButton(
                f"{r['kind']}: {r['label']}"[:60],
                callback_data=f"ur:{r['kind']}:{r['value_id']}",
            )
        ]
        for r in rules[:20]
    ]
    await update.message.reply_text(
        "Remove which rule?", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def cb_unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, kind, value_id = query.data.split(":", 2)
    removed = await db.remove_rule(query.message.chat_id, kind, value_id)
    await query.edit_message_text("Rule removed." if removed else "That rule was already gone.")


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = date.today()
    rows = await db.upcoming_for_chat(
        update.effective_chat.id, today, today + timedelta(days=config.DIGEST_WINDOW_DAYS)
    )
    text = tracker.format_digest(rows, today, config.DIGEST_WINDOW_DAYS)
    await update.message.reply_text(
        text or "Nothing dated in your window yet.",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    catalog = context.bot_data["catalog"]
    lines = ["<b>Status</b>", f"Sources: {', '.join(catalog.sources) or 'none'}"]
    for row in await db.job_status():
        when = row["last_run_at"].strftime("%d %b %H:%M UTC") if row["last_run_at"] else "never"
        lines.append(f"{row['job']}: {row['last_status']} @ {when}")
    tracked = await db.list_subscriptions(update.effective_chat.id)
    lines.append(f"You track {len(tracked)} game(s).")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def on_error(update, context):
    logger.exception("handler error", exc_info=context.error)


async def _post_init(app):
    """Runs inside PTB's own event loop.

    The asyncpg pool must be created on the loop that will use it, so it is
    built here rather than in main() - creating it on a different loop causes
    'attached to a different loop' failures under polling.
    """
    await db.init(config.DATABASE_URL)
    app.bot_data.setdefault("catalog", _catalog())
    logger.info(
        "startup complete — database ready, sources: %s",
        ", ".join(app.bot_data["catalog"].sources) or "NONE CONFIGURED",
    )


async def _post_shutdown(app):
    catalog = app.bot_data.get("catalog")
    if catalog is not None:
        await catalog.aclose()
    await db.close()


def build_application():
    app = (
        Application.builder()
        .token(config.TELEGRAM_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
    )
    if config.USE_WEBHOOK:
        app = app.updater(None)
    app = app.build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("track", cmd_track))
    app.add_handler(CommandHandler("untrack", cmd_untrack))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("upcoming", cmd_upcoming))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("unwatch", cmd_unwatch))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(cb_track, pattern=r"^tr:"))
    app.add_handler(CallbackQueryHandler(cb_untrack, pattern=r"^un:"))
    app.add_handler(CallbackQueryHandler(cb_unwatch, pattern=r"^ur:"))
    app.add_error_handler(on_error)
    return app


# --------------------------------------------------------------------------
# Webhook + scheduler endpoint
# --------------------------------------------------------------------------
def _webhook_secret():
    return hashlib.sha256(config.TELEGRAM_TOKEN.encode()).hexdigest()


def _presented_token(request):
    """Cron token from a header, falling back to a query param.

    The header form is preferred: query strings leak into access logs and
    browser history, headers generally don't.
    """
    return request.headers.get("X-Cron-Token") or request.query_params.get("token", "")


async def serve_webhook(app):
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, PlainTextResponse
    from starlette.routing import Route
    import uvicorn

    secret = _webhook_secret()
    url_path = secret[:32]
    job_lock = asyncio.Lock()

    async def telegram(request: Request):
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
            return PlainTextResponse("forbidden", status_code=403)
        try:
            payload = await request.json()
        except ValueError:
            return PlainTextResponse("bad request", status_code=400)
        await app.update_queue.put(Update.de_json(payload, app.bot))
        return PlainTextResponse("ok")

    async def run_task(request: Request):
        token = _presented_token(request)
        # Constant-time compare so the endpoint can't be probed byte by byte.
        if not config.CRON_TOKEN or not hmac.compare_digest(token, config.CRON_TOKEN):
            return PlainTextResponse("forbidden", status_code=403)
        job = request.query_params.get("job", "daily")
        if job not in {"daily", "weekly", "all"}:
            return PlainTextResponse("unknown job", status_code=400)

        if job_lock.locked():
            return JSONResponse({"status": "already running", "job": job}, status_code=409)

        async def runner():
            async with job_lock:
                notifier = tracker.Notifier(app.bot)
                try:
                    summary = await tracker.run_job(
                        job, app.bot_data["catalog"], notifier
                    )
                    logger.info("job %s finished: %s", job, summary)
                except Exception:
                    logger.exception("job %s failed", job)

        # Answer immediately: a full run can outlive the scheduler's HTTP
        # timeout, and the result is recorded in job_runs either way.
        asyncio.create_task(runner())
        return JSONResponse({"status": "started", "job": job}, status_code=202)

    async def task_status(request: Request):
        token = _presented_token(request)
        if not config.CRON_TOKEN or not hmac.compare_digest(token, config.CRON_TOKEN):
            return PlainTextResponse("forbidden", status_code=403)
        rows = await db.job_status()
        return JSONResponse(
            {
                r["job"]: {
                    "status": r["last_status"],
                    "at": r["last_run_at"].isoformat() if r["last_run_at"] else None,
                    "detail": r["detail"],
                }
                for r in rows
            }
        )

    async def health(request: Request):
        return PlainTextResponse("ok")

    routes = [
        Route(f"/{url_path}", telegram, methods=["POST"]),
        Route("/tasks/run", run_task, methods=["GET", "POST"]),
        Route("/tasks/status", task_status, methods=["GET"]),
        Route("/healthz", health, methods=["GET"]),
    ]
    server = uvicorn.Server(
        uvicorn.Config(
            Starlette(routes=routes),
            host="0.0.0.0",
            port=config.PORT,
            log_level="warning",
        )
    )

    # `async with app` initializes the bot, but PTB only invokes post_init /
    # post_shutdown from run_polling() and run_webhook(). We drive the
    # lifecycle ourselves here, so they must be called explicitly - otherwise
    # the database pool is never created and every handler raises
    # "db.init() has not been called". Both are idempotent, so the builder
    # hooks used by polling mode can stay registered.
    async with app:
        await _post_init(app)
        await app.bot.set_webhook(
            url=f"{config.WEBHOOK_BASE_URL}/{url_path}",
            secret_token=secret,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        logger.info("webhook registered; serving on 0.0.0.0:%s", config.PORT)
        await app.start()
        try:
            await server.serve()
        finally:
            await app.stop()
            await _post_shutdown(app)


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------
async def run_once(job):
    """Execute a scheduled job and exit - for local testing or a paid cron."""
    from telegram import Bot

    await db.init(config.DATABASE_URL)
    catalog = _catalog()
    bot = Bot(config.TELEGRAM_TOKEN)
    async with bot:
        notifier = tracker.Notifier(bot)
        summary = await tracker.run_job(job, catalog, notifier)
    print(f"{job}: {summary}")
    await catalog.aclose()
    await db.close()


async def run_server():
    # db + catalog are created by _post_init on this loop, and torn down by
    # _post_shutdown when the context manager exits.
    await serve_webhook(build_application())


def main():
    parser = argparse.ArgumentParser(description="GameWatch Telegram bot")
    parser.add_argument(
        "--job",
        choices=["daily", "weekly", "all"],
        help="run a scheduled job once and exit",
    )
    args = parser.parse_args()

    problems = config.validate()
    if problems:
        raise SystemExit("Configuration error:\n  - " + "\n  - ".join(problems))

    if args.job:
        asyncio.run(run_once(args.job))
        return

    if config.USE_WEBHOOK:
        asyncio.run(run_server())
        return

    # Local development: long polling, no HTTP server. run_polling owns the
    # loop and triggers post_init, which sets up the pool and catalog.
    # Use --job to exercise the scheduled work by hand.
    logger.info("starting long polling")
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
