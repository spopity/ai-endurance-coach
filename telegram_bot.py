"""
Telegram front-end for the agent graph. Uses long polling, so nothing on the
LAN has to be exposed - the bot dials out to Telegram.

Only user IDs listed in TELEGRAM_ALLOWED_USER_IDS (comma-separated) get a
response; everyone else is logged and ignored, since replies contain personal
health data and every query costs model time.

Run as its own process (deploy/ai-coach-telegram.service) so a bot crash or
restart never takes the web UI down with it.
"""
import asyncio
import logging
import os
import secrets
import time
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, PicklePersistence, filters,
)

from core.briefing import build_briefing
from core.api_log import trigger
from core.chat import AGENT_DISPLAY_NAMES, SYNC_WINDOW_DAYS, ask_supervisor, new_turn_id, run_sync, turn_proposal
from core.history import MAX_HISTORY_MESSAGES
from core.telegram_format import md_to_telegram_html, split_message
from core.db import load_env
from core import availability, feedback, strava
from core.next_session import (
    ProposalError, apply_proposal, discard_proposal, latest_decision, replan as replan_next_session, session_line,
    why_line,
)
from core.strava_notes import build_note, find_noteable, load_activity, publish_note, require_write_scope, strava_url
from logging_config import setup_logging

logger = logging.getLogger("AI_COACH_V1.telegram")

# Per-chat conversation history lives in context.chat_data and is pickled to
# disk, so a bot restart doesn't wipe the thread mid-conversation.
STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
PERSISTENCE_PATH = os.path.join(STATE_DIR, "telegram_bot.pickle")

# After this long without a message, the next one starts a fresh conversation -
# a question this evening shouldn't be interpreted against this morning's thread.
CONVERSATION_IDLE_SECONDS = 3 * 60 * 60

# Scheduled messages (times are local to the athlete, not the UTC server).
LOCAL_TZ = ZoneInfo("Europe/Bratislava")
DEFAULT_BRIEFING_TIME = "07:00"
BRIEFING_SYNC_DAYS = 3

HELP_TEXT = (
    "Ask me anything about your training - I'll route it to the right specialist.\n\n"
    f"I remember the last {MAX_HISTORY_MESSAGES // 2} exchanges, so follow-ups like \"and tomorrow?\" work. "
    f"The thread resets after {CONVERSATION_IDLE_SECONDS // 3600}h of silence.\n\n"
    "/twin <scenario> - send a what-if straight to the Athlete Twin (projections, climb times, tapers)\n"
    "/local - toggle Local only: answer with the local model on the home GPU instead of Gemini\n"
    "/strava [YYYY-MM-DD] [context] - (re)write the coach note on a Strava activity (latest activity by default), "
    "previewed before posting\n"
    "/briefing - today's briefing now (it also arrives every morning)\n"
    "To change your next session, agree it with the coach and say \"update my plan\" - "
    "nothing changes until you tap Apply.\n"
    "Tell me when you can't train (\"sick for 3 days\", \"away until Sunday\", \"only 40 min today\") - "
    "I save the exact dates and plan around them; normal training resumes by itself afterwards. "
    "Moving or swapping sessions between days works the same way (\"long ride Thursday instead of Tuesday\").\n"
    "After each hard ride or run I ask for RPE and how your legs feel; easy sessions and gym are covered by the "
    "quick morning check-in.\n"
    "/reset - start a fresh conversation\n"
    "/sync - pull the latest activities + wellness from Intervals.icu\n"
    "/help - show this message"
)


async def send_markdown_to(bot, chat_id: int, text: str):
    """Sends Markdown as Telegram HTML (split to fit), falling back to plain text if rejected.
    Works without an incoming update, so scheduled jobs use it too. Returns the sent messages."""
    sent = []
    for chunk in split_message(text):
        try:
            sent.append(await bot.send_message(chat_id, md_to_telegram_html(chunk), parse_mode=ParseMode.HTML,
                                               disable_web_page_preview=True))
        except BadRequest as e:
            logger.warning(f"Telegram rejected HTML formatting ({e}); resending as plain text.")
            sent.append(await bot.send_message(chat_id, chunk, disable_web_page_preview=True))
    return sent


async def send_markdown(update: Update, text: str):
    await send_markdown_to(update.get_bot(), update.effective_chat.id, text)


async def keep_typing(bot, chat_id: int, stop: asyncio.Event):
    """Telegram's typing indicator expires after ~5s, so refresh it until the reply is ready."""
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception as e:
            logger.debug(f"send_action failed: {e}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=4)
        except asyncio.TimeoutError:
            pass


# --- Conversation history ---

def get_history(chat_data: dict) -> list[dict]:
    """Returns this chat's recent turns, starting fresh if the thread has gone idle."""
    if time.time() - chat_data.get("last_activity", 0) > CONVERSATION_IDLE_SECONDS:
        chat_data["history"] = []
    return chat_data.setdefault("history", [])


def remember_turn(chat_data: dict, question: str, agent: str, reply: str):
    history = chat_data.setdefault("history", [])
    history.append({"role": "user", "content": question})
    history.append({"role": "assistant", "content": reply, "agent": agent})
    del history[:-MAX_HISTORY_MESSAGES]
    chat_data["last_activity"] = time.time()


# --- Handlers ---

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data["history"] = []
    await update.effective_message.reply_text("🧹 Conversation cleared - starting fresh.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP_TEXT)


async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Sync triggered from Telegram.")
    await update.effective_message.reply_text(f"Syncing the last {SYNC_WINDOW_DAYS} days from Intervals.icu...")
    stop = asyncio.Event()
    typing = asyncio.create_task(keep_typing(context.bot, update.effective_chat.id, stop))
    try:
        summary = await run_sync(SYNC_WINDOW_DAYS)
        logger.info(f"Sync complete: {summary}")
        await update.effective_message.reply_text(f"✅ {summary}")
    except Exception as e:
        logger.error(f"Sync from Telegram failed: {e}", exc_info=True)
        await update.effective_message.reply_text(f"❌ Sync failed: {e}")
    finally:
        stop.set()
        await typing


async def answer(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, forced_agent: str | None = None):
    """Runs one question through the graph (optionally pinned to one agent) and replies."""
    stop = asyncio.Event()
    typing = asyncio.create_task(keep_typing(context.bot, update.effective_chat.id, stop))
    try:
        history = get_history(context.chat_data)
        local_only = bool(context.chat_data.get("local_only"))
        turn_id = new_turn_id()
        with trigger("chat"):
            agent, reply = await ask_supervisor(text, history, agent=forced_agent, local_only=local_only,
                                                turn_id=turn_id)
        logger.info(f"Agent '{agent}' replied ({len(reply)} chars, {len(history)} prior messages); sending to Telegram.")
        if reply:
            remember_turn(context.chat_data, text, agent, reply)
        header = f"*→ {AGENT_DISPLAY_NAMES.get(agent, agent)}*{' · local' if local_only else ''}\n\n"
        await send_markdown(update, header + (reply or "(The agent returned an empty reply.)"))
        proposal = await asyncio.to_thread(turn_proposal, turn_id)
        if proposal:
            await update.effective_message.reply_text(proposal_text(proposal), reply_markup=proposal_buttons(proposal["id"]))
    except Exception as e:
        logger.error("Error handling Telegram message", exc_info=True)
        await update.effective_message.reply_text(f"❌ An error occurred: {e}")
    finally:
        stop.set()
        await typing


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.effective_message.text
    logger.info(f"Received Telegram message: {text!r}")
    await answer(update, context, text)


async def cmd_twin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip()
    if not text:
        await update.effective_message.reply_text(
            "Usage: /twin <scenario>\n\nExamples:\n"
            "/twin where will my CTL be after 6 weeks at 450 TSS/week?\n"
            "/twin how long would Alpe d'Huez take me at 90% FTP, and 2 kg lighter?\n"
            "/twin simulate a 10-day taper before a race on the 25th"
        )
        return
    logger.info(f"Received Telegram /twin request: {text!r}")
    await answer(update, context, text, forced_agent="athlete_twin")


async def cmd_local(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/local toggles Local only for this chat; /local on|off sets it. Persisted with chat_data."""
    arg = (context.args[0].lower() if context.args else "")
    current = bool(context.chat_data.get("local_only"))
    context.chat_data["local_only"] = {"on": True, "off": False}.get(arg, not current)
    if context.chat_data["local_only"]:
        await update.effective_message.reply_text(
            "🖥 Local only is ON - every answer comes from the local model on your GPU (free, a bit less thorough). "
            "Send /local again to allow Gemini.")
    else:
        await update.effective_message.reply_text("☁️ Local only is OFF - Coach, Twin and Racing use Gemini again.")


# --- Next-session changes agreed in chat: the coach proposes, the athlete applies ---

def proposal_text(p: dict) -> str:
    return (f"📝 Change your next session to:\n{p['session']}" + (f"\nWhy: {p['why']}" if p.get("why") else "")
            + "\n\nApply updates the dashboard, morning briefing and Strava notes.")


def proposal_buttons(pid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Apply", callback_data=f"nextsession:apply:{pid}"),
        InlineKeyboardButton("✖ Discard", callback_data=f"nextsession:discard:{pid}"),
    ]])


async def on_next_session_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_user.id not in context.bot_data.get("allowed_ids", []):
        await query.answer("Not allowed.")
        return
    _, action, pid = query.data.split(":", 2)
    try:
        if action == "apply":
            await asyncio.to_thread(apply_proposal, int(pid))
            note = "✅ Applied - this is now your next session everywhere."
        else:
            await asyncio.to_thread(discard_proposal, int(pid))
            note = "✖ Discarded - your next session is unchanged."
    except ProposalError as e:
        note = f"⚠️ Not changed: {e}"
    except Exception as e:
        logger.error("Next-session proposal button failed", exc_info=True)
        note = f"❌ Failed: {e}"
    logger.info(f"Next-session proposal #{pid} {action} from Telegram: {note}")
    await query.answer()
    await query.edit_message_text(query.message.text + "\n\n" + note)


# --- Strava coach notes: /strava with preview + buttons ---

def strava_buttons(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Post", callback_data=f"strava:post:{key}"),
        InlineKeyboardButton("🔄 Rewrite", callback_data=f"strava:redo:{key}"),
        InlineKeyboardButton("✖ Cancel", callback_data=f"strava:cancel:{key}"),
    ]])


def strava_preview_text(a: dict, note: str) -> str:
    when = a["start_time"].astimezone(LOCAL_TZ)
    return f"Strava note preview for {a['type']} \"{a['name']}\" ({when:%a %d %b}):\n\n{note}"


async def cmd_strava(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/strava [YYYY-MM-DD] [extra context] - previews a (re)written coach note for a Strava activity."""
    args = list(context.args)
    on_day = None
    if args:
        try:
            on_day = date.fromisoformat(args[0])
            args = args[1:]
        except ValueError:
            pass
    notes = " ".join(args).strip() or None

    a = await asyncio.to_thread(find_noteable, on_day)
    if not a:
        await update.effective_message.reply_text(
            f"No activity found{f' on {on_day}' if on_day else ''}. Usage: /strava [YYYY-MM-DD] [extra context]")
        return

    stop = asyncio.Event()
    typing = asyncio.create_task(keep_typing(context.bot, update.effective_chat.id, stop))
    try:
        note = await asyncio.to_thread(build_note, a, notes)
    except Exception as e:
        logger.error("Building Strava note failed", exc_info=True)
        await update.effective_message.reply_text(f"❌ Couldn't build the note: {e}")
        return
    finally:
        stop.set()
        await typing
    key = secrets.token_hex(4)
    # notes=None means "keep the context given last time" (build_note fell back to it).
    context.chat_data.setdefault("strava_previews", {})[key] = {
        "external_id": a["external_id"], "note": note, "notes": notes if notes is not None else a.get("strava_note_context"),
    }
    await update.effective_message.reply_text(strava_preview_text(a, note), reply_markup=strava_buttons(key),
                                              disable_web_page_preview=True)


async def on_strava_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_user.id not in context.bot_data.get("allowed_ids", []):
        await query.answer("Not allowed.")
        return
    _, action, key = query.data.split(":", 2)
    preview = context.chat_data.get("strava_previews", {}).get(key)
    if not preview:
        await query.answer("This preview has expired - send /strava again.")
        await query.edit_message_reply_markup(None)
        return

    if action == "cancel":
        context.chat_data["strava_previews"].pop(key, None)
        await query.answer("Cancelled.")
        await query.edit_message_text(query.message.text + "\n\n✖ Cancelled - nothing posted.")
        return

    a = await asyncio.to_thread(load_activity, preview["external_id"])
    if action == "redo":
        await query.answer("Rewriting...")
        note = await asyncio.to_thread(build_note, a, preview["notes"] or "")
        preview["note"] = note
        await query.edit_message_text(strava_preview_text(a, note), reply_markup=strava_buttons(key),
                                      disable_web_page_preview=True)
        return

    await query.answer("Posting to Strava...")
    try:
        require_write_scope()
        token = await asyncio.to_thread(strava.access_token)
        strava_id = await asyncio.to_thread(publish_note, a, preview["note"], token, preview["notes"] or "")
    except Exception as e:
        logger.error("Posting Strava note failed", exc_info=True)
        await query.edit_message_text(query.message.text + f"\n\n❌ Posting failed: {e}")
        return
    context.chat_data["strava_previews"].pop(key, None)
    logger.info(f"Strava note posted for {preview['external_id']} from Telegram.")
    await query.edit_message_text(query.message.text + f"\n\n✅ Posted to Strava: {strava_url(strava_id)}",
                                  disable_web_page_preview=True)


# --- Scheduled morning briefing ---
# (Activities are synced every 30 min by the host cron hitting /sync-data; the bot sends nothing for them.)

async def deliver_briefing(bot, chat_id: int, chat_data: dict):
    """Syncs, builds the briefing and sends it - and adds it to the chat history, so the
    athlete can reply with follow-ups ("why rest today?")."""
    stop = asyncio.Event()
    typing = asyncio.create_task(keep_typing(bot, chat_id, stop))
    try:
        try:
            await run_sync(BRIEFING_SYNC_DAYS)  # last night's HRV + yesterday's sessions
        except Exception as e:
            logger.warning(f"Pre-briefing sync failed, using stored data: {e}")
        text = await build_briefing()
        await send_markdown_to(bot, chat_id, text)
        get_history(chat_data)  # resets the thread first if it went idle
        remember_turn(chat_data, "(morning briefing)", "ai_coach", text)
        logger.info(f"Briefing sent to {chat_id}.")
        await send_checkin(bot, chat_id)
    except Exception as e:
        logger.error("Briefing failed", exc_info=True)
        await bot.send_message(chat_id, f"❌ Morning briefing failed: {e}")
    finally:
        stop.set()
        await typing


async def job_briefing(context: ContextTypes.DEFAULT_TYPE):
    await deliver_briefing(context.bot, context.job.chat_id, context.chat_data)


# --- Athlete feedback (core/feedback.py): RPE + legs after each session, morning check-in ---
# A few taps each. Answers that change readiness re-plan the next session and say so.

FEEDBACK_EVERY_MINUTES = 10
QUIET_FROM_HOUR, QUIET_UNTIL_HOUR = 22, 7  # local time: no session questions at night


def _local_today() -> date:
    return datetime.now(LOCAL_TZ).date()


def rpe_buttons(eid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(str(n), callback_data=f"fb:rpe:{eid}:{n}") for n in row]
                                 for row in ((1, 2, 3, 4, 5), (6, 7, 8, 9, 10))])


def legs_buttons(eid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"fb:legs:{eid}:{key}")
                                  for key, label in feedback.LEGS.items()]])


def body_buttons(day: str) -> InlineKeyboardMarkup:
    labels = {**feedback.BODY, "ill": "🤒 Ill"}
    keys = list(labels)
    return InlineKeyboardMarkup([[InlineKeyboardButton(labels[k], callback_data=f"fb:body:{day}:{k}") for k in keys[:4]],
                                 [InlineKeyboardButton(labels[keys[4]], callback_data=f"fb:body:{day}:{keys[4]}")]])


def motivation_buttons(day: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"fb:mot:{day}:{key}")
                                  for key, label in feedback.MOTIVATION.items()]])


async def send_checkin(bot, chat_id: int):
    day = _local_today()
    if await asyncio.to_thread(feedback.today_checkin, day):
        return
    await asyncio.to_thread(feedback.mark_checkin_asked, day)
    start = datetime.combine(day - timedelta(days=1), dtime(0), LOCAL_TZ)
    yesterday = await asyncio.to_thread(feedback.checkin_sessions, start, start + timedelta(days=1))
    after = (" after yesterday's " + ", ".join(f"{a['name']} ({a['type']})" for a in yesterday)) if yesterday else ""
    await bot.send_message(chat_id, f"☀️ Quick check-in - how does your body feel this morning{after}?",
                           reply_markup=body_buttons(day.isoformat()))


async def job_session_feedback(context: ContextTypes.DEFAULT_TYPE):
    hour = datetime.now(LOCAL_TZ).hour
    if hour >= QUIET_FROM_HOUR or hour < QUIET_UNTIL_HOUR:
        return
    for a in await asyncio.to_thread(feedback.sessions_to_ask):
        await asyncio.to_thread(feedback.mark_asked, a["external_id"])
        minutes = round((a["moving_time_sec"] or 0) / 60)
        await context.bot.send_message(
            context.job.chat_id,
            f"🏁 {a['name']} ({a['type']}, {minutes} min)\nHow hard was it? RPE 1 = very easy ... 10 = all out",
            reply_markup=rpe_buttons(a["external_id"]))
        logger.info(f"Asked for feedback on {a['external_id']}.")


async def replan_and_send(bot, chat_id: int, reason: str):
    """Your answer changed things: recompute the week and the next session, and send it."""
    await asyncio.to_thread(replan_next_session)
    d = await asyncio.to_thread(latest_decision)
    if d:
        why = why_line(d["decision"])
        await send_markdown_to(bot, chat_id, f"🔄 {reason} - next session updated:\n**{session_line(d['decision'])}**"
                               + (f"\n_{why}_" if why else ""))


async def on_feedback_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_user.id not in context.bot_data.get("allowed_ids", []):
        await query.answer("Not allowed.")
        return
    _, step, key, value = query.data.split(":", 3)
    chat_id = query.message.chat_id
    await query.answer()
    try:
        if step == "rpe":
            await asyncio.to_thread(feedback.save_session, key, rpe=int(value))
            await query.edit_message_text(f"{query.message.text.splitlines()[0]}\nRPE {value} ✓ - and your legs?",
                                          reply_markup=legs_buttons(key))
        elif step == "legs":
            await asyncio.to_thread(feedback.save_session, key, legs=value)
            await query.edit_message_text(f"{query.message.text.splitlines()[0]}\n"
                                          f"{query.message.text.splitlines()[-1].split(' - ')[0]}, legs "
                                          f"{feedback.LEGS[value].lower()} ✓ Thanks!")
            _, amber = await asyncio.to_thread(feedback.signals)
            if any("last hard session" in r for r in amber):
                await replan_and_send(context.bot, chat_id, "That session took more out of you than planned")
        elif step == "body":
            day = date.fromisoformat(key)
            await asyncio.to_thread(feedback.save_checkin, day, body=value)
            if value == "ill":
                await asyncio.to_thread(availability.add, "sick", day, 1, None, "morning check-in")
                await query.edit_message_text(
                    f"🤒 Noted - ill today ({day:%a %d %b}), no training. Get well! If it lasts longer, just tell me "
                    "(e.g. \"sick for 3 days\") and I'll keep the dates; normal training resumes by itself after them.")
                await replan_and_send(context.bot, chat_id, "You're ill today")
            else:
                await query.edit_message_text(f"Body: {feedback.BODY[value].lower()} ✓ - and motivation?",
                                              reply_markup=motivation_buttons(key))
        elif step == "mot":
            day = date.fromisoformat(key)
            await asyncio.to_thread(feedback.save_checkin, day, motivation=value)
            c = await asyncio.to_thread(feedback.today_checkin, day)
            await query.edit_message_text(f"Body: {feedback.BODY.get(c['body'], '?').lower()}, motivation: "
                                          f"{feedback.MOTIVATION[value].lower()} ✓ Thanks!")
            if c["body"] == "very_sore" or (c["body"] == "sore" and value == "low"):
                await replan_and_send(context.bot, chat_id, "Taking your check-in into account")
    except Exception as e:
        logger.error("Feedback button failed", exc_info=True)
        await context.bot.send_message(chat_id, f"❌ Couldn't save that: {e}")


async def cmd_briefing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Briefing requested via /briefing.")
    await deliver_briefing(context.bot, update.effective_chat.id, context.chat_data)


async def on_unauthorized(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.warning(
        f"Ignoring Telegram update from unauthorized user id={user.id if user else '?'} "
        f"username={user.username if user else '?'}"
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled Telegram bot error", exc_info=context.error)


async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("briefing", "Today's briefing now"),
        BotCommand("twin", "Ask the Athlete Twin a what-if directly"),
        BotCommand("local", "Toggle Local only (home GPU instead of Gemini)"),
        BotCommand("strava", "(Re)write the coach note on a Strava activity"),
        BotCommand("reset", "Start a fresh conversation"),
        BotCommand("sync", "Pull latest activities + wellness"),
        BotCommand("help", "What can I ask?"),
    ])
    me = await application.bot.get_me()
    logger.info(f"Telegram bot @{me.username} is up (long polling).")


def main():
    setup_logging("telegram.log")
    env = load_env()

    token = env.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set in .env")
    allowed_ids = [int(x) for x in env.get("TELEGRAM_ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x]
    if not allowed_ids:
        raise SystemExit("TELEGRAM_ALLOWED_USER_IDS is not set in .env - refusing to run an open bot.")

    allowed = filters.User(user_id=allowed_ids)
    os.makedirs(STATE_DIR, exist_ok=True)
    persistence = PicklePersistence(filepath=PERSISTENCE_PATH)
    application = Application.builder().token(token).persistence(persistence).post_init(post_init).build()
    application.bot_data["allowed_ids"] = allowed_ids

    application.add_handler(CommandHandler(["start", "help"], cmd_help, filters=allowed))
    application.add_handler(CommandHandler("twin", cmd_twin, filters=allowed))
    application.add_handler(CommandHandler("local", cmd_local, filters=allowed))
    application.add_handler(CommandHandler("strava", cmd_strava, filters=allowed))
    application.add_handler(CallbackQueryHandler(on_strava_button, pattern=r"^strava:"))
    application.add_handler(CallbackQueryHandler(on_next_session_button, pattern=r"^nextsession:"))
    application.add_handler(CallbackQueryHandler(on_feedback_button, pattern=r"^fb:"))
    application.add_handler(CommandHandler("reset", cmd_reset, filters=allowed))
    application.add_handler(CommandHandler("sync", cmd_sync, filters=allowed))
    application.add_handler(CommandHandler("briefing", cmd_briefing, filters=allowed))
    application.add_handler(MessageHandler(allowed & filters.TEXT & ~filters.COMMAND, on_message))
    application.add_handler(MessageHandler(~allowed, on_unauthorized))
    application.add_error_handler(on_error)

    hh, mm = (int(x) for x in (env.get("BRIEFING_TIME") or DEFAULT_BRIEFING_TIME).split(":"))
    briefing_at = dtime(hh, mm, tzinfo=LOCAL_TZ)
    for chat_id in allowed_ids:  # private chat id == user id
        application.job_queue.run_daily(job_briefing, briefing_at, chat_id=chat_id, name=f"briefing-{chat_id}")
        application.job_queue.run_repeating(job_session_feedback, FEEDBACK_EVERY_MINUTES * 60, first=60,
                                            chat_id=chat_id, name=f"feedback-{chat_id}")
    logger.info(f"Briefing scheduled daily at {briefing_at:%H:%M} {LOCAL_TZ.key}; "
                f"session feedback checked every {FEEDBACK_EVERY_MINUTES} min.")

    logger.info(f"Starting Telegram bot for {len(allowed_ids)} allowed user(s)...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
