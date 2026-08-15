"""Pure-logic check for tag building/validation. Run: python test_tag.py"""
import os

os.environ.setdefault("BOT_TOKEN", "x")
os.environ.setdefault("GROUP_CHAT_ID", "1")

from bot import build_tag, validate_tag, MAX_TAG_LEN  # noqa: E402

assert build_tag("SLS", "MGR") == "SLS-MGR"
assert validate_tag(build_tag("SLS", "MGR")) is None
assert validate_tag("x" * MAX_TAG_LEN) is None
assert validate_tag("x" * (MAX_TAG_LEN + 1)) is not None
assert validate_tag("héllo") is not None  # non-ascii rejected
assert validate_tag("") is None  # clearing a tag is valid

print("ok")
