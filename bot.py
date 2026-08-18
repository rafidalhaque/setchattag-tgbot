"""Telegram bot: department/role selection -> chat member tag (Bot API 10.2).

Setup:
  1. pip install -e .
  2. Copy .env.example to .env, fill BOT_TOKEN and TARGET_GROUP_ID.
  3. Bot must already be admin in TARGET_GROUP_ID with can_manage_tags -- grant
     via promoteChatMember(can_manage_tags=True) from another admin account
     first. The bot cannot grant this right to itself.
  4. Run: python bot.py

Ephemeral messages (Bot API 10.2): as of PTB 22.8 (installed here), none of
this surface is wrapped yet -- BotCommand has no is_ephemeral field, Bot
methods take no receiver_user_id, there's no edit_ephemeral_message_*/
delete_ephemeral_message, and Message doesn't model ephemeral_message_id.
Everything ephemeral below goes through raw_api() (Bot._post) as a stub;
swap to the real wrapped methods once PTB ships them.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Awaitable

from aiolimiter import AsyncLimiter
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.ext import (
    AIORateLimiter,
    Application,
    BaseUpdateProcessor,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
TARGET_GROUP_ID = int(os.environ["TARGET_GROUP_ID"])  # <-- real target group chat_id (set in .env)

DB_PATH = Path(os.environ.get("DB_PATH", str(Path(__file__).with_name("members.db"))))

# --- department/role config: edit this to change the menu ------------------
# dept_code -> (display name, {role_code: role display name})

_COMMON_ROLES: dict[str, str] = {
    "সম্পাদক": "সম্পাদক",
    "সহকারী": "সহকারী সম্পাদক",
    "সদস্য": "সদস্য",
}

DEPARTMENTS: dict[str, tuple[str, dict[str, str]]] = {
    "কেন্দ্র": ("কেন্দ্র", {"কেন্দ্রীয় সভাপতি": "কেন্দ্রীয় সভাপতি", "সেক্রেটারি জেনারেল": "সেক্রেটারি জেনারেল"}),
    "দপ্তর": ("দপ্তর", _COMMON_ROLES),
    "সাহিত্য": ("সাহিত্য", _COMMON_ROLES),
    "প্রকাশনা": ("প্রকাশনা", _COMMON_ROLES),
    "দাওয়াহ": ("দাওয়াহ", _COMMON_ROLES),
    "এইচআরএম": ("এইচআরএম", _COMMON_ROLES),
    "ফাউন্ডেশন": ("ফাউন্ডেশন", _COMMON_ROLES),
    "অর্থ": ("অর্থ", _COMMON_ROLES),
    "কলেজ": ("কলেজ", _COMMON_ROLES),
    "শিক্ষা": ("শিক্ষা", _COMMON_ROLES),
    "ব্যবসায় শিক্ষা": ("ব্যবসা শিক্ষা", _COMMON_ROLES),
    "গবেষণা": ("উচ্চশিক্ষা ও গবেষণা", _COMMON_ROLES),
    "তথ্যপ্রযুক্তি": ("তথ্যপ্রযুক্তি", _COMMON_ROLES),
    "শিশুকল্যাণ": ("শিশুকল্যাণ", _COMMON_ROLES),
    "প্রচার": ("প্রচার", _COMMON_ROLES),
    "সমাজসেবা": ("সমাজসেবা", _COMMON_ROLES),
    "তথ্য": ("তথ্য", _COMMON_ROLES),
    "এইচআরডি": ("এইচআরডি", _COMMON_ROLES),
    "মাদরাসা": ("মাদরাসা", _COMMON_ROLES),
    "আইন": ("আইন", _COMMON_ROLES),
    "ছাত্রকল্যাণ": ("ছাত্রকল্যাণ", _COMMON_ROLES),
    "বিজ্ঞান": ("বিজ্ঞান", _COMMON_ROLES),
    "স্পোর্টস": ("স্পোর্টস", _COMMON_ROLES),
    "পিআর": ("পাবলিক রিলেশনস", _COMMON_ROLES),
    "ছাত্র অধিকার": ("ছাত্র অধিকার", _COMMON_ROLES),
    "আন্তর্জাতিক": ("আন্তর্জাতিক", _COMMON_ROLES),
    "স্কুল": ("স্কুল", _COMMON_ROLES),
    "পাঠাগার": ("পাঠাগার", _COMMON_ROLES),
    "মিডিয়া": ("মিডিয়া", _COMMON_ROLES),
    "প্লানিং": ("প্লানিং এন্ড ডেভেলপমেন্ট", _COMMON_ROLES),
    "বিতর্ক": ("বিতর্ক", _COMMON_ROLES),
    "শিল্প ও সংস্কৃতি": ("শিল্প ও সংস্কৃতি", _COMMON_ROLES)
}
# -----------------------------------------------------------------------------

# callback_data is capped at 64 bytes by Telegram; Bengali dept/role codes blow past
# that when combined (e.g. "r:{dept}:{role}"), so buttons carry index positions instead.
DEPT_CODES = list(DEPARTMENTS.keys())

SELECT_DEPARTMENT, SELECT_ROLE = range(2)
MAX_TAG_LEN = 16
CONVERSATION_TIMEOUT = 120  # seconds idle before the picker is torn down

# setChatMemberTag itself emits a chat_member update shaped like a real join
# (left/kicked -> member), which on_join would otherwise mistake for one.
# Track users we just tagged ourselves and skip the resulting event.
RECENTLY_TAGGED: dict[int, float] = {}
RECENT_TAG_WINDOW = 10  # seconds
EPHEMERAL_COMMANDS = {"setrole": True, "cancel": True, "mytag": True, "start": True}
RATE_LIMIT_MSG = "টেলিগ্রাম এই মুহূর্তে ব্যস্ত। কিছুক্ষণ পর /setrole আবার চেষ্টা করুন।"
NON_MEMBER_MSG = "আপনি গ্রুপের সদস্য নন। গ্রুপ এডমিনের সাথে যোগাযোগ করুন।"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Single reused connection instead of one-per-call (the old `with sqlite3.connect(...) as con`
# only manages the transaction, not the connection -- it never closed). check_same_thread=False
# because asyncio.to_thread runs each call on a threadpool thread, not the connection's creation
# thread; _DB_LOCK below is what actually keeps access to it serialized, so that's safe.
_DB_LOCK = asyncio.Lock()
_con = sqlite3.connect(DB_PATH, check_same_thread=False)
_con.execute("PRAGMA journal_mode=WAL")


def init_db() -> None:
    _con.execute(
        """CREATE TABLE IF NOT EXISTS members (
            tg_id INTEGER PRIMARY KEY,
            first_name TEXT,
            last_name TEXT,
            username TEXT,
            department TEXT NOT NULL,
            role TEXT NOT NULL
        )"""
    )
    _con.commit()


def _save_member_sync(user, dept: str, role: str) -> None:
    _con.execute(
        """INSERT INTO members (tg_id, first_name, last_name, username, department, role)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(tg_id) DO UPDATE SET
             first_name=excluded.first_name, last_name=excluded.last_name,
             username=excluded.username, department=excluded.department, role=excluded.role""",
        (user.id, user.first_name, user.last_name, user.username, dept, role),
    )
    _con.commit()


async def save_member(user, dept: str, role: str) -> None:
    async with _DB_LOCK:
        await asyncio.to_thread(_save_member_sync, user, dept, role)


def _get_member_sync(tg_id: int) -> tuple[str, str] | None:
    row = _con.execute(
        "SELECT department, role FROM members WHERE tg_id = ?", (tg_id,)
    ).fetchone()
    return tuple(row) if row else None


async def get_member(tg_id: int) -> tuple[str, str] | None:
    async with _DB_LOCK:
        return await asyncio.to_thread(_get_member_sync, tg_id)


def build_tag(dept: str, role: str) -> str:
    # if dept != "কেন্দ্র":
    #     return f"{role},{dept}"
    return f"{dept}" # tag = dept name only to avoid character limit exceed

def validate_tag(tag: str) -> str | None:
    """Return an error message if tag is invalid, else None."""
    if len(tag) > MAX_TAG_LEN:
        return f"Tag '{tag}' is {len(tag)} chars, max {MAX_TAG_LEN}."
    # if not tag.isascii():
    #     return f"Tag '{tag}' has non-ASCII/emoji characters."
    return None


def department_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(name, callback_data=f"d:{i}")]
        for i, (name, _roles) in enumerate(DEPARTMENTS.values())
    ]
    return InlineKeyboardMarkup(rows)


def role_keyboard(dept: str) -> InlineKeyboardMarkup:
    dept_idx = DEPT_CODES.index(dept)
    _name, roles = DEPARTMENTS[dept]
    rows = [
        [InlineKeyboardButton(rname, callback_data=f"r:{dept_idx}:{ridx}")]
        for ridx, rname in enumerate(roles.values())
    ]
    rows.append([InlineKeyboardButton("বর্তমান ট্যাগ মুছুন", callback_data=f"x:{dept_idx}")])
    return InlineKeyboardMarkup(rows)


# --- raw Bot API 10.2 ephemeral-message stubs (not wrapped by PTB 22.8) ----

# ExtBot._do_post() routes every bot._post() call (named methods AND raw calls like
# this one, everything except getUpdates) through self.rate_limiter.process_request()
# automatically -- confirmed by reading the installed PTB 22.8 source directly, not
# assumed. So raw_api() doesn't need its own wrap; it already shares _base_limiter
# with every other call the moment _make_rate_limiter() points _base_limiter at this
# instance. overall_max_rate=0 on AIORateLimiter's own construction just prevents it
# from building a second, separate bucket that raw_api() would otherwise never use.
_RAW_API_LIMITER = AsyncLimiter(25, 1)

async def raw_api(bot, method: str, params: dict):
    """... rate-limited via ExtBot._do_post -> shared _base_limiter, no separate wrap needed."""
    return await bot._post(method, params)


async def register_commands(app: Application) -> None:
    # BotCommand has no is_ephemeral field in installed PTB -> raw payload.
    commands = [
        {"command": cmd, "description": desc, "is_ephemeral": EPHEMERAL_COMMANDS.get(cmd, False)}
        for cmd, desc in [
            ("start", "শুরু করুন"),
            ("setrole", "আপনার দায়িত্ব ও বিভাগ সিলেক্ট করুন"),
            ("mytag", "বর্তমান ট্যাগ দেখুন"),
            ("cancel", "বর্তমান অবস্থা বাতিল করুন"),
        ]
    ]
    await raw_api(app.bot, "setMyCommands", {"commands": commands})


def _is_group(chat) -> bool:
    return chat.type in ("group", "supergroup")


async def cleanup_ephemeral(context: ContextTypes.DEFAULT_TYPE) -> None:
    eid = context.user_data.pop("ephemeral_message_id", None)
    chat_id = context.user_data.pop("ephemeral_chat_id", None)
    receiver_user_id = context.user_data.pop("ephemeral_user_id", None)
    if not eid:
        return
    try:
        await raw_api(
            context.bot,
            "deleteEphemeralMessage",
            {"chat_id": chat_id, "receiver_user_id": receiver_user_id, "ephemeral_message_id": eid},
        )
    except (BadRequest, Forbidden, RetryAfter) as e:
        logger.warning("couldn't delete stray ephemeral picker %s: %s", eid, e)


async def _fallback_retry_notice(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Best-effort plain DM telling the user to retry -- used when the ephemeral
    picker itself couldn't be delivered (flood limit, transient error), so the
    failure is never silent even though AIORateLimiter already retried once."""
    try:
        await context.bot.send_message(
            user_id, "দুঃখিত, এই মুহূর্তে সিস্টেম ব্যস্ত। কিছুক্ষণ পর /setrole আবার চেষ্টা করুন।"
        )
    except (BadRequest, Forbidden, RetryAfter):
        pass


async def _contain_public_leak(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, user_id: int) -> None:
    """A picker meant for `user_id` alone (Bot API 10.2 ephemeral send) came back
    without ephemeral_message_id but WITH a normal message_id -- Telegram accepted
    it as a plain PUBLIC message in the group instead. That message is live right
    now and visible to everyone in the group. This is a privacy incident, not an
    ordinary delivery failure, so it gets its own loud, greppable log line instead
    of folding into the generic warning -- and we delete the leaked message before
    doing anything else.

    Uses the wrapped delete_message (not raw_api) on purpose: it goes through
    AIORateLimiter, which auto-retries once on RetryAfter (max_retries=1 in
    main()) -- the same rate-limited conditions that caused the leak are exactly
    the conditions where the delete itself is most likely to also get throttled,
    so the path with a built-in retry is the more reliable one here.
    """
    logger.error(
        "PRIVACY LEAK: ephemeral picker for user %s posted as PUBLIC message_id=%s in chat %s -- deleting now",
        user_id, message_id, chat_id,
    )
    try:
        await context.bot.delete_message(chat_id, message_id)
    except (BadRequest, Forbidden, RetryAfter) as e:
        logger.critical(
            "PRIVACY LEAK UNCONTAINED: could not delete leaked picker message_id=%s in chat %s for user %s: %s",
            message_id, chat_id, user_id, e,
        )


async def send_picker(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, markup: InlineKeyboardMarkup) -> int:
    """Send the department picker. Ephemeral (receiver_user_id) inside the
    group so only the tapper sees it; a normal DM reply in private chats.
    Must stay fast and blocking-call-free -- Telegram only accepts the
    ephemeral reply within 15s of the triggering command. AIORateLimiter
    (max_retries=1) already retries once on RetryAfter before this sees it,
    so any exception/failure here means that retry didn't help either.
    """
    chat, user = update.effective_chat, update.effective_user
    if not _is_group(chat):
        await update.effective_message.reply_text(text, reply_markup=markup)
        return SELECT_DEPARTMENT

    try:
        result = await raw_api(
            context.bot,
            "sendMessage",
            {"chat_id": chat.id, "receiver_user_id": user.id, "text": text, "reply_markup": markup},
        )
    except (BadRequest, Forbidden, RetryAfter) as e:
        logger.warning("ephemeral picker failed to deliver to %s: %s", user.id, e)  # e.g. user offline, still rate limited
        await _fallback_retry_notice(context, user.id)
        return ConversationHandler.END

    eid = result.get("ephemeral_message_id") if isinstance(result, dict) else 0
    if not eid:
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if message_id:
            # Telegram sent something, just not ephemerally -- it's live in the group now.
            await _contain_public_leak(context, chat.id, message_id, user.id)
        else:
            logger.warning("sendMessage returned no ephemeral_message_id for user %s: %r", user.id, result)
        await _fallback_retry_notice(context, user.id)
        return ConversationHandler.END
    context.user_data["ephemeral_message_id"] = eid
    context.user_data["ephemeral_chat_id"] = chat.id
    context.user_data["ephemeral_user_id"] = user.id
    return SELECT_DEPARTMENT


async def edit_picker(query, context: ContextTypes.DEFAULT_TYPE, text: str, markup: InlineKeyboardMarkup | None) -> bool:
    """Edit the in-flight picker/confirmation, ephemeral or plain to match how it was sent.
    Returns False (instead of raising) on flood/permission/network failure, so callers whose
    flow depends on the edit landing (e.g. on_department switching to the role keyboard) can
    fail the conversation gracefully instead of leaving the user stuck on stale buttons.
    """
    eid = context.user_data.get("ephemeral_message_id")
    if not eid:
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except (BadRequest, Forbidden, RetryAfter) as e:
            logger.warning("couldn't edit picker for %s: %s", query.from_user.id, e)
            return False
        return True

    chat_id = context.user_data["ephemeral_chat_id"]
    receiver_user_id = context.user_data["ephemeral_user_id"]
    # text always changes here; markup=None means "clear the keyboard", not "skip the text",
    # so this always needs editEphemeralMessageText (pass an empty markup to drop the buttons).
    try:
        await raw_api(
            context.bot,
            "editEphemeralMessageText",
            {
                "chat_id": chat_id,
                "receiver_user_id": receiver_user_id,
                "ephemeral_message_id": eid,
                "text": text,
                "reply_markup": markup or InlineKeyboardMarkup([]),
            },
        )
    except (BadRequest, Forbidden, RetryAfter) as e:
        logger.warning("couldn't edit ephemeral picker %s for %s: %s", eid, receiver_user_id, e)
        return False
    return True


async def setrole(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await is_group_member(context.bot, TARGET_GROUP_ID, update.effective_user.id):
        await update.effective_message.reply_text(NON_MEMBER_MSG)
        return ConversationHandler.END
    # The incoming /setrole command can itself be an ephemeral command;
    # PTB doesn't model that field, so read it from the raw update payload.
    incoming_eid = update.message.api_kwargs.get("ephemeral_message_id", 0) if update.message else 0
    if incoming_eid:
        logger.info(
            "setrole invoked as ephemeral command (id=%s) update_id=%s user_id=%s",
            incoming_eid, update.update_id,
            update.effective_user.id if update.effective_user else None,
        )
    # also wired as a fallback so re-running /setrole restarts a stuck conversation
    # (e.g. user deleted the picker message client-side -- no update for that, so the
    # conversation stays parked in its old state until this or the timeout clears it)
    await cleanup_ephemeral(context)
    return await send_picker(update, context, "আপনার বিভাগ কোনটি?", department_keyboard())


async def on_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_chat.id != TARGET_GROUP_ID:
        return ConversationHandler.END

    actor = update.chat_member.from_user
    if actor and actor.id == context.bot.id:
        return ConversationHandler.END  # bot-caused change (e.g. setChatMemberTag), not a real join

    old, new = update.chat_member.old_chat_member, update.chat_member.new_chat_member
    if new.status not in ("member", "administrator") or old.status == new.status:
        return ConversationHandler.END
    tagged_at = RECENTLY_TAGGED.get(new.user.id)
    if tagged_at is not None and time.monotonic() - tagged_at < RECENT_TAG_WINDOW:
        return ConversationHandler.END
    try:
        # Exempt from the send_picker leak: chat_id here is new.user.id (a private DM chat),
        # never TARGET_GROUP_ID -- there is no group message for Telegram to mis-deliver
        # publicly, ephemeral or otherwise. This never goes through raw_api()/send_picker.
        await context.bot.send_message(
            new.user.id, "আসসালামু আলাইকুম ওয়া রাহমাতুল্লাহ। আপনার বিভাগ কোনটি?", reply_markup=department_keyboard()
        )
    except (Forbidden, RetryAfter):
        return ConversationHandler.END  # ponytail: user never DM'd the bot, they can run /setrole later
    return SELECT_DEPARTMENT


async def on_department(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    dept = DEPT_CODES[int(query.data.split(":", 1)[1])]
    ok = await edit_picker(query, context, f"বিভাগ: {DEPARTMENTS[dept][0]}। আপনার দায়িত্ব কি?:", role_keyboard(dept))
    if not ok:
        # buttons on screen are still the stale department picker, but the conversation
        # would otherwise move to SELECT_ROLE where those callback_data values don't match
        # any handler -- end it cleanly instead of leaving the user stuck.
        await cleanup_ephemeral(context)
        await _fallback_retry_notice(context, query.from_user.id)
        return ConversationHandler.END
    return SELECT_ROLE


async def is_group_member(bot, target_group_id: int, user_id: int) -> bool:
    """Defensive membership check -- any getChatMember failure counts as not-a-member."""
    try:
        member = await bot.get_chat_member(target_group_id, user_id)
    except (BadRequest, Forbidden) as e:
        logger.info("membership check failed for %s: %s", user_id, e)
        return False
    if member.status == "restricted":
        return bool(member.is_member)
    return member.status in ("creator", "administrator", "member")


async def apply_tag(query, context, dept: str, tag: str, role_label: str) -> None:
    err = validate_tag(tag)
    if err:
        await edit_picker(query, context, f"ট্যাগ যুক্ত করা যাচ্ছে না। সমস্যা: {err}", None)
        return

    user = query.from_user
    try:
        member = await context.bot.get_chat_member(TARGET_GROUP_ID, user.id)
    except RetryAfter as e:
        logger.warning("rate limited checking membership for %s: %s", user.id, e)
        await edit_picker(query, context, RATE_LIMIT_MSG, None)
        return
    except (BadRequest, Forbidden):
        member = None
    is_member = member is not None and (
        member.status in ("creator", "administrator", "member")
        or (member.status == "restricted" and member.is_member)
    )
    is_admin = member is not None and member.status in ("creator", "administrator")

    if not is_member:
        await edit_picker(query, context, NON_MEMBER_MSG, None)
        return

    RECENTLY_TAGGED[user.id] = time.monotonic()
    try:
        if hasattr(context.bot, "set_chat_member_tag"):
            await context.bot.set_chat_member_tag(TARGET_GROUP_ID, user.id, tag)
        else:
            # ponytail: installed PTB predates the wrapped method, hit the raw API
            # (routed through raw_api() so it's covered by _RAW_API_LIMITER too)
            await raw_api(
                context.bot,
                "setChatMemberTag",
                {"chat_id": TARGET_GROUP_ID, "user_id": user.id, "tag": tag},
            )
    except RetryAfter as e:
        logger.warning("rate limited tagging %s: %s", user.id, e)
        await edit_picker(query, context, RATE_LIMIT_MSG, None)
        return
    except Forbidden:
        await edit_picker(
            query, context, "আমি ট্যাগ যুক্ত করতে পারছি না। এডমিনকে বলুন আমাকে can_manage_tags পার্মিশন দিতে।.", None
        )
        return
    except BadRequest as e:
        if "chat_creator_required" in str(e).lower():
            try:
                await context.bot.set_chat_administrator_custom_title(TARGET_GROUP_ID, user.id, tag)
            except (BadRequest, Forbidden, RetryAfter) as title_err:
                logger.info("admin custom title fallback failed for %s: %s", user.id, title_err)
                # Telegram's real restriction here differs by who the target is, not just
                # "creator_required" -- the earlier setChatMemberTag error text is the same
                # fixed string for both, so branch on actual status to give accurate guidance
                # instead of always blaming the owner.
                if member.status == "creator":
                    msg = (
                        "টেলিগ্রামের রেস্ট্রিকশনের কারণে গ্রুপ owner-এর কাস্টম টাইটেল বট দিয়ে পরিবর্তন করা যায় না। "
                        "owner নিজে গ্রুপ সেটিংস থেকে নিজের টাইটেল পরিবর্তন করতে পারবেন।"
                    )
                else:
                    msg = (
                        "অন্য একজন এডমিন আপনাকে প্রোমোট করেছেন বলে আমি আপনার কাস্টম টাইটেল পরিবর্তন করতে পারছি না। "
                        "যিনি আপনাকে এডমিন বানিয়েছেন তাকে অনুরোধ করুন আপনার টাইটেল পরিবর্তন করে দিতে।"
                    )
                await edit_picker(query, context, msg, None)
            else:
                if tag:
                    await save_member(user, dept, role_label)
                    await edit_picker(query, context, f"জাজাকাল্লাহু খাইর। আপনার বর্তমান টাইটেল: {tag}", None)
                else:
                    await edit_picker(query, context, "টাইটেল রিমোভ করা হয়েছে।", None)
                return
        else:
            await edit_picker(query, context, f"টেলিগ্রাম এই ট্যাগটি রিজেক্ট করেছে: {e}", None)
        return

    if is_admin:
        try:
            await context.bot.set_chat_administrator_custom_title(TARGET_GROUP_ID, user.id, tag)
        except (BadRequest, Forbidden, RetryAfter) as e:
            # ponytail: expected for admins/owner not promoted by this bot, Telegram rejects it outright
            logger.info("admin custom title sync skipped for %s: %s", user.id, e)

    if tag:
        await save_member(user, dept, role_label)
        await edit_picker(query, context, f"জাজাকাল্লাহু খাইর। আপনার বর্তমান ট্যাগ: {tag}", None)
    else:
        await edit_picker(query, context, "ট্যাগ রিমোভ করা হয়েছে।", None)


async def on_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _prefix, dept_idx, role_idx = query.data.split(":", 2)
    dept = DEPT_CODES[int(dept_idx)]
    role = list(DEPARTMENTS[dept][1].keys())[int(role_idx)]
    await apply_tag(query, context, dept, build_tag(dept, role), role)
    context.user_data.pop("ephemeral_message_id", None)
    context.user_data.pop("ephemeral_chat_id", None)
    context.user_data.pop("ephemeral_user_id", None)
    return ConversationHandler.END


async def on_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    dept = DEPT_CODES[int(query.data.split(":", 1)[1])]
    await apply_tag(query, context, dept, "", "")
    context.user_data.pop("ephemeral_message_id", None)
    context.user_data.pop("ephemeral_chat_id", None)
    context.user_data.pop("ephemeral_user_id", None)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await is_group_member(context.bot, TARGET_GROUP_ID, update.effective_user.id):
        await update.effective_message.reply_text(NON_MEMBER_MSG)
        return ConversationHandler.END
    await cleanup_ephemeral(context)
    await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


async def on_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await cleanup_ephemeral(context)
    return ConversationHandler.END


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not await is_group_member(context.bot, TARGET_GROUP_ID, user.id):
        await update.effective_message.reply_text(NON_MEMBER_MSG)
        return
    stored = await get_member(user.id)
    if stored:
        await update.effective_message.reply_text(
            f"আসসালামু আলাইকুম ওয়া রাহমাতুল্লাহ! আপনার বর্তমান ট্যাগ: {build_tag(*stored)}। "
            "পরিবর্তন করতে /setrole দিন।"
        )
    else:
        await update.effective_message.reply_text(
            "আসসালামু আলাইকুম ওয়া রাহমাতুল্লাহ! আপনার দায়িত্ব ও বিভাগ সেট করতে /setrole কমান্ড দিন।"
        )


async def mytag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    try:
        member = await context.bot.get_chat_member(TARGET_GROUP_ID, user.id)
    except (BadRequest, Forbidden) as e:
        logger.info("membership check failed for %s: %s", user.id, e)
        member = None
    is_member = member is not None and (
        member.status in ("creator", "administrator", "member")
        or (member.status == "restricted" and member.is_member)
    )
    if not is_member:
        await update.effective_message.reply_text(NON_MEMBER_MSG)
        return
    tag = getattr(member, "tag", None)
    if not tag:
        stored = await get_member(user.id)
        tag = build_tag(*stored) if stored else None
    await update.effective_message.reply_text(f"আপনার ট্যাগ: {tag}" if tag else "আপনার ট্যাগ এখনো যুক্ত করা হয়নি। /setrole কমান্ড ব্যবহার করুন।")


class PerUserUpdateProcessor(BaseUpdateProcessor):
    """Lets different users' updates run concurrently (up to max_concurrent_updates in
    flight) but serializes updates from the *same* user.

    ConversationHandler here uses per_chat=False/per_user=True, so its state key is just
    the user id -- but that only decides which bucket an update belongs to, it doesn't
    serialize access to that bucket. ConversationHandler._conversations is a plain dict:
    check_update() reads the current state synchronously, and the state write
    (_update_state) only happens after the matched handler's callback has awaited its
    Telegram API calls. Under concurrent_updates, two updates from the same user in
    flight at once (double-tapped button, redelivered update, impatient /setrole retry)
    would both read the same pre-update state and could both act on it -- e.g. two
    picker messages, or apply_tag running twice. Different users don't share a state key,
    so they don't race each other; only same-user in-flight overlap does, and this lock
    closes exactly that gap without touching the ConversationHandler flow itself.
    ponytail: per-user asyncio.Lock, dict grows with distinct users seen (fine at this
    bot's scale); add TTL/LRU eviction if the member base grows into the tens of thousands.
    """

    def __init__(self, max_concurrent_updates: int) -> None:
        super().__init__(max_concurrent_updates)
        self._user_locks: dict[int, asyncio.Lock] = {}

    async def do_process_update(self, update: object, coroutine: Awaitable) -> None:
        user = update.effective_user if isinstance(update, Update) else None
        if user is None:
            await coroutine
            return
        lock = self._user_locks.setdefault(user.id, asyncio.Lock())
        async with lock:
            await coroutine

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass


def _make_rate_limiter() -> AIORateLimiter:
    # group_max_rate=0: AIORateLimiter's default (20/min) is keyed by chat_id, and every
    # ephemeral picker uses the same TARGET_GROUP_ID chat_id even though each one is only
    # visible to one user -- so the default would cap the whole bot at ~20 pickers/minute
    # regardless of how many distinct users are tapping /setrole. We don't know Telegram's
    # real server-side flood limit for receiver-scoped ephemeral sends (Bot API 10.2 is very
    # new), so rather than guess a number, disable the self-imposed per-chat cap and rely on
    # the shared overall bucket (see _RAW_API_LIMITER) plus genuine RetryAfter from Telegram --
    # which max_retries=1 now retries once, and every call site below falls back to a
    # user-facing message if that retry isn't enough.
    #
    # overall_max_rate=0 disables AIORateLimiter's own internal AsyncLimiter (it would
    # otherwise build a second, independent 30/s bucket); _base_limiter is then pointed at
    # _RAW_API_LIMITER below so this and raw_api() share one real token bucket instead of two
    # additive ones. _base_limiter is a declared __slot__ on AIORateLimiter (checked against
    # installed PTB 22.8's telegram/ext/_aioratelimiter.py), not a property, so plain
    # assignment after construction is exactly how the class's own __init__ sets it.
    limiter = AIORateLimiter(max_retries=1, group_max_rate=0, overall_max_rate=0)
    limiter._base_limiter = _RAW_API_LIMITER
    return limiter


def main() -> None:
    init_db()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .rate_limiter(_make_rate_limiter())
        # 64: high enough that 100 users tapping /setrole around the same time aren't queued
        # behind each other (each handler is I/O-bound: a couple Telegram calls + one to_thread
        # DB call), low enough not to open hundreds of tasks/DB-threadpool-slots at once.
        # PerUserUpdateProcessor still serializes any single user's own updates -- see above.
        .concurrent_updates(PerUserUpdateProcessor(64))
        .post_init(register_commands)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("setrole", setrole, filters=filters.ChatType.PRIVATE),
            ChatMemberHandler(on_join, ChatMemberHandler.CHAT_MEMBER),
        ],
        states={
            SELECT_DEPARTMENT: [CallbackQueryHandler(on_department, pattern=r"^d:")],
            SELECT_ROLE: [
                CallbackQueryHandler(on_role, pattern=r"^r:"),
                CallbackQueryHandler(on_remove, pattern=r"^x:"),
            ],
            ConversationHandler.TIMEOUT: [CallbackQueryHandler(on_timeout), MessageHandler(filters.ALL, on_timeout)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel, filters=filters.ChatType.PRIVATE),
            CommandHandler("setrole", setrole, filters=filters.ChatType.PRIVATE),
        ],
        per_chat=False,  # join event fires in the group chat, replies happen in DM -- track by user only
        conversation_timeout=CONVERSATION_TIMEOUT,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("mytag", mytag, filters=filters.ChatType.PRIVATE))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
