# Task: Telegram bot — department/role selection → member tag

Build a feature for a python-telegram-bot (PTB v22.7+) bot that:

## Flow
1. On a command (e.g. /setrole) or on joining the group, the bot DMs or messages
   the user with an InlineKeyboardMarkup listing departments (no free text entry).
2. User taps a department → bot edits the message to show an InlineKeyboardMarkup
   of roles scoped to that department (no free text entry).
3. User taps a role → bot builds a short tag string from department+role and calls
   the Bot API method setChatMemberTag to set it as the user's member tag in the
   target group/supergroup.

## Hard constraints (Bot API 10.1, setChatMemberTag)
- Tag must be ≤16 characters, no emoji. Use short codes/abbreviations for
  departments and roles (not full display names) when building the tag —
  validate length before calling the API and reject/truncate with a clear
  error if a combination would exceed 16 chars.
- Only works in groups/supergroups (not private chats/channels) — the target
  chat_id must be the group, even if the selection UI runs in a DM.
- The bot must be an administrator in that group with the can_manage_tags
  right (granted via promoteChatMember(can_manage_tags=True) beforehand;
  this is a one-time setup step, not something the bot can grant itself).
- Passing tag="" clears an existing tag — support a "remove my tag" option too.
- Confirm whether your installed PTB version exposes a wrapped
  `bot.set_chat_member_tag(chat_id, user_id, tag)` method (tag support landed
  in PTB objects as of v22.7) — if not yet wrapped in your version, fall back
  to a raw request via `bot._post("setChatMemberTag", {...})` or upgrade PTB.
- use .env to store secrets and ids

## Implementation requirements
- Use `ConversationHandler` with two states: SELECT_DEPARTMENT, SELECT_ROLE.
- Departments and roles come from a fixed config (dict of department -> list
  of roles), not user text input — every step is InlineKeyboardButton taps
  with callback_data.
- callback_data should encode short identifiers, not full labels, to stay
  under Telegram's 64-byte callback_data limit (e.g. "dept:SLS", "role:SLS:MGR").
- Look up the user's numeric user_id from `update.effective_user.id` and the
  target group chat_id from config (or from where the flow was triggered).
- After a successful setChatMemberTag call, edit the message to confirm the
  tag that was set, and persist the department/role choice (DB or file) in
  case you need to reapply tags later (e.g. bot restarts, tag gets cleared).
- Wrap the API call in try/except — handle:
  - 403 (bot lacks can_manage_tags) → tell the user to contact an admin.
  - 400 (tag too long / invalid / chat isn't a group or supergroup).
- Add a `/mytag` command to show the user's current tag via getChatMember.
- use sqlite db to store: tg's first name and last name, telegram username (nullable), telegram id (not null), department (not null), role (not null)

## Deliverable
A single Python module (or clearly separated handlers file) with the
ConversationHandler registered, the department/role config as a clearly
editable constant near the top of the file, and inline comments marking
where to plug in the real group chat_id and department/role data.