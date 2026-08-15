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

import logging
import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
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
    "ব্যবসা": ("ব্যবসা", _COMMON_ROLES),
    "উচ্চশিক্ষা": ("উচ্চশিক্ষা", _COMMON_ROLES),
    "গবেষণা": ("গবেষণা", _COMMON_ROLES),
    "আইটি": ("তথ্যপ্রযুক্তি", _COMMON_ROLES),
    "শিশু": ("শিশুকল্যাণ", _COMMON_ROLES),
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
    "ছাত্রঅধিকার": ("ছাত্র অধিকার", _COMMON_ROLES),
    "আন্তর্জাতিক": ("আন্তর্জাতিক", _COMMON_ROLES),
    "স্কুল": ("স্কুল", _COMMON_ROLES),
    "পাঠাগার": ("পাঠাগার", _COMMON_ROLES),
    "মিডিয়া": ("মিডিয়া", _COMMON_ROLES),
    "প্লানিং": ("প্লানিং এন্ড ডেভেলপমেন্ট", _COMMON_ROLES),
    "বিতর্ক": ("বিতর্ক", _COMMON_ROLES),
    "সংস্কৃতি": ("সাংস্কৃতিক", _COMMON_ROLES)
}
# -----------------------------------------------------------------------------

SELECT_DEPARTMENT, SELECT_ROLE = range(2)
MAX_TAG_LEN = 16
CONVERSATION_TIMEOUT = 120  # seconds idle before the picker is torn down
EPHEMERAL_COMMANDS = {"setrole": True, "cancel": True, "mytag": False}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS members (
                tg_id INTEGER PRIMARY KEY,
                first_name TEXT,
                last_name TEXT,
                username TEXT,
                department TEXT NOT NULL,
                role TEXT NOT NULL
            )"""
        )


def save_member(user, dept: str, role: str) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """INSERT INTO members (tg_id, first_name, last_name, username, department, role)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(tg_id) DO UPDATE SET
                 first_name=excluded.first_name, last_name=excluded.last_name,
                 username=excluded.username, department=excluded.department, role=excluded.role""",
            (user.id, user.first_name, user.last_name, user.username, dept, role),
        )


def get_member(tg_id: int) -> tuple[str, str] | None:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT department, role FROM members WHERE tg_id = ?", (tg_id,)
        ).fetchone()
    return tuple(row) if row else None


def build_tag(dept: str, role: str) -> str:
    if dept != "কেন্দ্র":
        return f"{role},{dept}"
    return f"{role}"

def validate_tag(tag: str) -> str | None:
    """Return an error message if tag is invalid, else None."""
    if len(tag) > MAX_TAG_LEN:
        return f"Tag '{tag}' is {len(tag)} chars, max {MAX_TAG_LEN}."
    # if not tag.isascii():
    #     return f"Tag '{tag}' has non-ASCII/emoji characters."
    return None


def department_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(name, callback_data=f"d:{code}")]
        for code, (name, _roles) in DEPARTMENTS.items()
    ]
    return InlineKeyboardMarkup(rows)


def role_keyboard(dept: str) -> InlineKeyboardMarkup:
    _name, roles = DEPARTMENTS[dept]
    rows = [
        [InlineKeyboardButton(rname, callback_data=f"r:{dept}:{rcode}")]
        for rcode, rname in roles.items()
    ]
    rows.append([InlineKeyboardButton("Remove my tag", callback_data=f"x:{dept}")])
    return InlineKeyboardMarkup(rows)


# --- raw Bot API 10.2 ephemeral-message stubs (not wrapped by PTB 22.8) ----

async def raw_api(bot, method: str, params: dict):
    """Call a Bot API method PTB hasn't wrapped yet. Bot._post serializes
    TelegramObject values (e.g. reply_markup=InlineKeyboardMarkup(...)) the
    same way every wrapped method does internally, and returns the decoded
    `result` JSON as-is (dict/list/bool) -- no Message/BotCommand typing.
    """
    return await bot._post(method, params)


async def register_commands(app: Application) -> None:
    # BotCommand has no is_ephemeral field in installed PTB -> raw payload.
    commands = [
        {"command": cmd, "description": desc, "is_ephemeral": EPHEMERAL_COMMANDS.get(cmd, False)}
        for cmd, desc in [
            ("setrole", "Pick your department & role"),
            ("mytag", "Show your current tag"),
            ("cancel", "Cancel the selection"),
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
    except (BadRequest, Forbidden) as e:
        logger.warning("couldn't delete stray ephemeral picker %s: %s", eid, e)


async def send_picker(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, markup: InlineKeyboardMarkup) -> int:
    """Send the department picker. Ephemeral (receiver_user_id) inside the
    group so only the tapper sees it; a normal DM reply in private chats.
    Must stay fast and blocking-call-free -- Telegram only accepts the
    ephemeral reply within 15s of the triggering command.
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
    except (BadRequest, Forbidden) as e:
        logger.warning("ephemeral picker failed to deliver to %s: %s", user.id, e)  # e.g. user offline
        return ConversationHandler.END

    eid = result.get("ephemeral_message_id") if isinstance(result, dict) else 0
    if not eid:
        logger.warning("sendMessage returned no ephemeral_message_id for user %s: %r", user.id, result)
        return ConversationHandler.END
    context.user_data["ephemeral_message_id"] = eid
    context.user_data["ephemeral_chat_id"] = chat.id
    context.user_data["ephemeral_user_id"] = user.id
    return SELECT_DEPARTMENT


async def edit_picker(query, context: ContextTypes.DEFAULT_TYPE, text: str, markup: InlineKeyboardMarkup | None) -> None:
    """Edit the in-flight picker/confirmation, ephemeral or plain to match how it was sent."""
    eid = context.user_data.get("ephemeral_message_id")
    if not eid:
        await query.edit_message_text(text, reply_markup=markup)
        return

    chat_id = context.user_data["ephemeral_chat_id"]
    receiver_user_id = context.user_data["ephemeral_user_id"]
    # text always changes here; markup=None means "clear the keyboard", not "skip the text",
    # so this always needs editEphemeralMessageText (pass an empty markup to drop the buttons).
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


async def setrole(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # The incoming /setrole command can itself be an ephemeral command;
    # PTB doesn't model that field, so read it from the raw update payload.
    incoming_eid = update.message.api_kwargs.get("ephemeral_message_id", 0) if update.message else 0
    if incoming_eid:
        logger.info("setrole invoked as ephemeral command (id=%s)", incoming_eid)
    # also wired as a fallback so re-running /setrole restarts a stuck conversation
    # (e.g. user deleted the picker message client-side -- no update for that, so the
    # conversation stays parked in its old state until this or the timeout clears it)
    await cleanup_ephemeral(context)
    return await send_picker(update, context, "Pick your department:", department_keyboard())


async def on_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_chat.id != TARGET_GROUP_ID:
        return ConversationHandler.END
    old, new = update.chat_member.old_chat_member, update.chat_member.new_chat_member
    if new.status not in ("member", "administrator") or old.status == new.status:
        return ConversationHandler.END
    try:
        await context.bot.send_message(
            new.user.id, "Welcome! Pick your department:", reply_markup=department_keyboard()
        )
    except Forbidden:
        return ConversationHandler.END  # ponytail: user never DM'd the bot, they can run /setrole later
    return SELECT_DEPARTMENT


async def on_department(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    dept = query.data.split(":", 1)[1]
    await edit_picker(query, context, f"{DEPARTMENTS[dept][0]} — pick your role:", role_keyboard(dept))
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
        await edit_picker(query, context, f"Can't set tag: {err}", None)
        return

    user = query.from_user
    if not await is_group_member(context.bot, TARGET_GROUP_ID, user.id):
        await edit_picker(
            query,
            context,
            "You're not a member of the internal group, so a tag can't be set. "
            "Contact an admin if you think this is wrong.",
            None,
        )
        return

    try:
        if hasattr(context.bot, "set_chat_member_tag"):
            await context.bot.set_chat_member_tag(TARGET_GROUP_ID, user.id, tag)
        else:
            # ponytail: installed PTB predates the wrapped method, hit the raw API
            await context.bot._post(
                "setChatMemberTag",
                {"chat_id": TARGET_GROUP_ID, "user_id": user.id, "tag": tag},
            )
    except Forbidden:
        await edit_picker(
            query, context, "I can't set tags here — ask a group admin to grant me can_manage_tags.", None
        )
        return
    except BadRequest as e:
        if "chat_creator_required" in str(e).lower():
            await edit_picker(
                query, context,
                "Can't set a tag for the group owner through the bot — only the owner's own "
                "account can do that. This is a Telegram restriction, not a bug.",
                None,
            )
        else:
            await edit_picker(query, context, f"Telegram rejected the tag: {e}", None)
        return

    if tag:
        save_member(user, dept, role_label)
        await edit_picker(query, context, f"Done! Your tag is now: {tag}", None)
    else:
        await edit_picker(query, context, "Tag removed.", None)


async def on_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _prefix, dept, role = query.data.split(":", 2)
    await apply_tag(query, context, dept, build_tag(dept, role), role)
    context.user_data.pop("ephemeral_message_id", None)
    context.user_data.pop("ephemeral_chat_id", None)
    context.user_data.pop("ephemeral_user_id", None)
    return ConversationHandler.END


async def on_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    dept = query.data.split(":", 1)[1]
    await apply_tag(query, context, dept, "", "")
    context.user_data.pop("ephemeral_message_id", None)
    context.user_data.pop("ephemeral_chat_id", None)
    context.user_data.pop("ephemeral_user_id", None)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await cleanup_ephemeral(context)
    await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


async def on_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await cleanup_ephemeral(context)
    return ConversationHandler.END


async def mytag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    try:
        member = await context.bot.get_chat_member(TARGET_GROUP_ID, user.id)
    except (BadRequest, Forbidden) as e:
        await update.effective_message.reply_text(f"Couldn't look you up: {e}")
        return
    tag = getattr(member, "tag", None)
    if not tag:
        stored = get_member(user.id)
        tag = build_tag(*stored) if stored else None
    await update.effective_message.reply_text(f"Your tag: {tag}" if tag else "You have no tag set.")


def main() -> None:
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(register_commands).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("setrole", setrole),
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
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("setrole", setrole)],
        per_chat=False,  # join event fires in the group chat, replies happen in DM -- track by user only
        conversation_timeout=CONVERSATION_TIMEOUT,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("mytag", mytag))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
