"""Telegram bot: department/role selection -> chat member tag (Bot API 10.1).

Setup:
  1. pip install -e .
  2. Copy .env.example to .env, fill BOT_TOKEN and GROUP_CHAT_ID.
  3. Bot must already be admin in GROUP_CHAT_ID with can_manage_tags -- grant
     via promoteChatMember(can_manage_tags=True) from another admin account
     first. The bot cannot grant this right to itself.
  4. Run: python bot.py
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
)

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
GROUP_CHAT_ID = int(os.environ["GROUP_CHAT_ID"])  # <-- real target group chat_id (set in .env)

DB_PATH = Path(os.environ.get("DB_PATH", str(Path(__file__).with_name("members.db"))))

# --- department/role config: edit this to change the menu ------------------
# dept_code -> (display name, {role_code: role display name})
DEPARTMENTS: dict[str, tuple[str, dict[str, str]]] = {
    "কেন্দ্র": ("কেন্দ্র", {"CP": "কেন্দ্রীয় সভাপতি", "SG": "সেক্রেটারি জেনারেল"}),
    "তথ্যপ্রযুক্তি": ("তথ্যপ্রযুক্তি", {"Sec": "Secretary", "Member": "Agent"}),
}
# -----------------------------------------------------------------------------

SELECT_DEPARTMENT, SELECT_ROLE = range(2)
MAX_TAG_LEN = 16

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
    return f"{dept}, {role}"


def validate_tag(tag: str) -> str | None:
    """Return an error message if tag is invalid, else None."""
    if len(tag) > MAX_TAG_LEN:
        return f"Tag '{tag}' is {len(tag)} chars, max {MAX_TAG_LEN}."
    if not tag.isascii():
        return f"Tag '{tag}' has non-ASCII/emoji characters."
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


async def setrole(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(
        "আপনার বিভাগ:", reply_markup=department_keyboard()
    )
    return SELECT_DEPARTMENT


async def on_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_chat.id != GROUP_CHAT_ID:
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
    await query.edit_message_text(
        f"{DEPARTMENTS[dept][0]} — pick your role:", reply_markup=role_keyboard(dept)
    )
    return SELECT_ROLE


async def apply_tag(query, context, dept: str, tag: str, role_label: str) -> None:
    err = validate_tag(tag)
    if err:
        await query.edit_message_text(f"Can't set tag: {err}")
        return

    user = query.from_user
    try:
        if hasattr(context.bot, "set_chat_member_tag"):
            await context.bot.set_chat_member_tag(GROUP_CHAT_ID, user.id, tag)
        else:
            # ponytail: installed PTB predates the wrapped method, hit the raw API
            await context.bot._post(
                "setChatMemberTag",
                {"chat_id": GROUP_CHAT_ID, "user_id": user.id, "tag": tag},
            )
    except Forbidden:
        await query.edit_message_text(
            "I can't set tags here — ask a group admin to grant me can_manage_tags."
        )
        return
    except BadRequest as e:
        await query.edit_message_text(f"Telegram rejected the tag: {e}")
        return

    if tag:
        save_member(user, dept, role_label)
        await query.edit_message_text(f"Done! Your tag is now: {tag}")
    else:
        await query.edit_message_text("Tag removed.")


async def on_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _prefix, dept, role = query.data.split(":", 2)
    await apply_tag(query, context, dept, build_tag(dept, role), role)
    return ConversationHandler.END


async def on_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    dept = query.data.split(":", 1)[1]
    await apply_tag(query, context, dept, "", "")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


async def mytag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    try:
        member = await context.bot.get_chat_member(GROUP_CHAT_ID, user.id)
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
    app = Application.builder().token(BOT_TOKEN).build()

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
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=False,  # join event fires in the group chat, replies happen in DM -- track by user only
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("mytag", mytag))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
