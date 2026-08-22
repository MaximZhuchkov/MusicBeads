"""
Bare-bones song relay bot (Russian UI, multi-line song parts).

Flow:
  1. Participants type /register in a group chat to register as a performer for
     THAT group (one user can be registered in multiple groups). /unregister
     removes them from that group's roster. Registrations are persisted to a
     local SQLite database, so they survive bot restarts.
  2. Someone types /playsong in the group -> bot picks a random song and DMs
     every active (non-snoozed) registered participant in that group asking
     whether they're ready to perform it, song name included. Anyone who taps
     "Нет" or doesn't respond within READINESS_CHECK_SECONDS (default 5
     minutes) is excluded from this song -- no part is assigned to them.
  3. Once every candidate has answered (or the 5 minutes are up, whichever
     comes first), the bot round-robin assigns parts among whoever said "Да"
     and DMs each their part(s). If nobody said "Да", the round is cancelled
     before it starts and no parts are assigned to anyone.
  4. If a participant has multiple parts, they're prompted one at a time. They can
     send a video circle repeatedly (each overwrites the last) and tap "Confirm"
     when happy, which locks in that part and advances to the next one.
  5. After ROUND_DURATION_SECONDS, the bot checks whether every part was confirmed.
     If yes, it posts each video circle to the group in song order. If no, it
     announces the song as incomplete and the round ends.
  6. At any point during an active round (or the readiness poll that precedes
     it), /cancel (typed in the group, or in DM by one of that round's
     participants) immediately ends it and notifies both the group and every
     participant who had a part or was asked about readiness.

Admin tools (group administrators/creator only) for dealing with inactive
registrants who'd otherwise leave a song perpetually incomplete:
  - /snooze: temporarily excludes a registered user from this group's rounds
    for a given duration (minutes/hours/days). They stay registered and
    rejoin automatically once the snooze expires -- no need to /register again.
  - /unsnooze: lifts a snooze early.
  - /unregister_user: completely removes another user's registration for this
    group. To participate again, they must /register themselves.
  All three target a user either by replying to one of their messages (most
  reliable) or with `@username`/a numeric id, resolved among users already
  registered in that group.

/stats (open to everyone, run in the group) gives a friendly, emoji-led
snapshot: who's registered, who's currently snoozed (and until when), and the
outcome of every song played since the bot last started (✅ success,
❌ timed out incomplete, 🚫 cancelled by an admin).

Song selection avoids repeats: each group won't be given the same song twice in
a row across separate /playsong runs until every song in songs/ has come up for
that group, at which point the rotation for that group resets. This tracking is
in-memory only -- it resets when the bot restarts, so a restart can hand out a
song that was already played in the previous run.

Song JSON schema (see songs/example_song.json):
  {
    "title": "...",
    "parts": [
      { "lines": ["line one", "line two"] },
      ...
    ]
  }
  Each part's "lines" is a list of strings so a single component can span
  multiple lines of lyrics. Parts are deliberately unlabeled (no verse/chorus
  distinction) and shown to participants only as numbered "Часть N" -- who's
  performing verse vs. chorus isn't something they need to know.

Known v1 simplifications (see chat for discussion):
  - No check that a /register'd user is actually a member of the target group
    (beyond having typed the command there).
  - Only one round can be active at a time, globally (across all groups).
  - Round state (in-progress assignments/submissions) is in-memory only -- a
    restart loses any in-progress round. Registrations themselves, however,
    are persisted (see the Persistence section below).
  - DB access uses plain synchronous sqlite3 calls. Given the tiny read/write
    volume here (a handful of registrations per command), the brief event-loop
    block is a non-issue; a busier bot should move this to a thread executor.
  - /playsong and /cancel are restricted to group administrators/creator
    (checked live via getChatMember at command time -- not cached, so a
    demotion/promotion takes effect on the very next command). /register and
    /unregister remain open to everyone. /snooze, /unsnooze, and
    /unregister_user are likewise admin-only.
  - Per-group "already played" song tracking is in-memory only (see module
    docstring above); it is deliberately NOT persisted to the DB. Note that a
    song counts as "played" for that exclusion the moment it's *selected* by
    /playsong, regardless of whether the round then succeeds, times out, or
    gets /cancel'd -- the separate song_history used by /stats records that
    same event again, but tagged with its eventual outcome.
  - @username resolution for /snooze, /unsnooze, and /unregister_user only
    works for users already registered in that group -- Telegram's Bot API
    has no general username-to-id lookup. Replying to the target's message
    always works regardless of this.
"""

import asyncio
import html
import json
import logging
import os
import random
import re
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from video_concat import VideoConcatError, build_concatenated_video, ffmpeg_available

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("song_bot")

# httpx logs full request URLs (including your bot token) at INFO level by default.
# Quiet it down so the token never ends up in your console output or log files.
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_TOKEN = os.environ.get("SONG_BOT_TOKEN")
SONGS_DIR = Path(__file__).parent / "songs"
PERKS_FILE = Path(__file__).parent / "perks"
DB_FILE = Path(os.environ.get("SONG_BOT_DB", str(Path(__file__).parent / "registrations.db")))
ROUND_DURATION_SECONDS = int(os.environ.get("ROUND_DURATION_SECONDS", 900))
READINESS_CHECK_SECONDS = int(os.environ.get("READINESS_CHECK_SECONDS", 60))
PERK_PROBABILITY = float(os.environ.get("PERK_PROBABILITY", 0.8))
VIDEO_NOTE_SEND_DELAY_SECONDS = float(os.environ.get("VIDEO_NOTE_SEND_DELAY_SECONDS", 2))

# ---------------------------------------------------------------------------
# Persistence (SQLite-backed registrations and snoozes)
# ---------------------------------------------------------------------------
#
# A registration is a (group_chat_id, user_id) pair -- a user can be registered
# in several groups at once, and each group has its own independent roster.
# A snooze is also (group_chat_id, user_id)-scoped: it temporarily excludes an
# otherwise-registered user from that group's rounds without unregistering
# them, and expires on its own once `until_ts` passes. Everything else
# (rounds, assignments, submissions) stays in-memory as before.


def init_db() -> None:
    """Create tables if they don't exist yet. Safe to call every startup."""
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS registrations (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                username TEXT,
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS snoozes (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                until_ts INTEGER NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS group_settings (
                chat_id INTEGER PRIMARY KEY,
                concat_enabled INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()


def load_registrations() -> dict[int, dict[int, dict]]:
    """Load all persisted registrations into the in-memory shape used at runtime:
    chat_id -> {user_id: {"name": display_name, "username": username_or_None}}.
    """
    result: dict[int, dict[int, dict]] = {}
    with sqlite3.connect(DB_FILE) as conn:
        for chat_id, user_id, display_name, username in conn.execute(
            "SELECT chat_id, user_id, display_name, username FROM registrations"
        ):
            result.setdefault(chat_id, {})[user_id] = {"name": display_name, "username": username}
    return result


def db_add_registration(chat_id: int, user_id: int, display_name: str, username: Optional[str]) -> None:
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "INSERT INTO registrations (chat_id, user_id, display_name, username) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (chat_id, user_id) DO UPDATE SET "
            "display_name = excluded.display_name, username = excluded.username",
            (chat_id, user_id, display_name, username),
        )
        conn.commit()


def db_remove_registration(chat_id: int, user_id: int) -> bool:
    """Deletes the registration (and any snooze for the same pair, since a
    snooze on someone who's no longer registered is meaningless). Returns True
    if a registration row actually existed.
    """
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute(
            "DELETE FROM registrations WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        )
        conn.execute("DELETE FROM snoozes WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        conn.commit()
        return cur.rowcount > 0


def load_snoozes() -> dict[int, dict[int, int]]:
    """Load all persisted snoozes into chat_id -> {user_id: until_ts (unix seconds, UTC)}."""
    result: dict[int, dict[int, int]] = {}
    with sqlite3.connect(DB_FILE) as conn:
        for chat_id, user_id, until_ts in conn.execute(
            "SELECT chat_id, user_id, until_ts FROM snoozes"
        ):
            result.setdefault(chat_id, {})[user_id] = until_ts
    return result


def db_set_snooze(chat_id: int, user_id: int, until_ts: int) -> None:
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "INSERT INTO snoozes (chat_id, user_id, until_ts) VALUES (?, ?, ?) "
            "ON CONFLICT (chat_id, user_id) DO UPDATE SET until_ts = excluded.until_ts",
            (chat_id, user_id, until_ts),
        )
        conn.commit()


def db_clear_snooze(chat_id: int, user_id: int) -> bool:
    """Returns True if a snooze row actually existed."""
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute(
            "DELETE FROM snoozes WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        )
        conn.commit()
        return cur.rowcount > 0


def load_concat_settings() -> dict[int, bool]:
    """Load every group's persisted post-round concatenated-video setting
    into chat_id -> enabled. A chat_id absent here has never had /concat run
    and defaults to disabled (see concat_command).
    """
    result: dict[int, bool] = {}
    with sqlite3.connect(DB_FILE) as conn:
        for chat_id, enabled in conn.execute("SELECT chat_id, concat_enabled FROM group_settings"):
            result[chat_id] = bool(enabled)
    return result


def db_set_concat_enabled(chat_id: int, enabled: bool) -> None:
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "INSERT INTO group_settings (chat_id, concat_enabled) VALUES (?, ?) "
            "ON CONFLICT (chat_id) DO UPDATE SET concat_enabled = excluded.concat_enabled",
            (chat_id, int(enabled)),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

# chat_id -> {user_id: {"name": display_name, "username": username_or_None}}.
# Mirrors the `registrations` DB table and is populated from it at startup by
# load_registrations(); every mutation below also writes through to the DB so
# the two stay in sync.
registered_users: dict[int, dict[int, dict]] = {}

# chat_id -> {user_id: until_ts (unix seconds, UTC)}. Mirrors the `snoozes` DB
# table the same way. A snoozed-but-still-registered user is skipped when
# assigning parts for a round until their snooze expires.
snoozed_until: dict[int, dict[int, int]] = {}

# chat_id -> whether this group has the post-round concatenated-video feature
# enabled (admin-toggled via /concat). Mirrors the `group_settings` DB table.
# Any chat_id absent here defaults to disabled -- see concat_command below.
concat_enabled: dict[int, bool] = {}

# chat_id -> set of song filenames already picked for that group. Intentionally
# NOT persisted -- starts empty every time the bot restarts (see module docstring).
played_songs: dict[int, set[str]] = {}

# chat_id -> list of {"title", "status", "timestamp"} in the order they happened.
# status is one of "success" / "incomplete" / "cancelled". Intentionally NOT
# persisted -- /stats only reports what happened since the current bot process
# started, same lifetime as played_songs above.
song_history: dict[int, list[dict]] = {}

SONG_STATUS_EMOJI = {
    "success": "\u2705",
    "incomplete": "\u274c",
    "cancelled": "\U0001F6AB",
    "no_participants": "\U0001F614",
}
SONG_STATUS_TEXT = {
    "success": "исполнена полностью",
    "incomplete": "не хватило времени/участников",
    "cancelled": "отменена администратором",
    "no_participants": "никто не подтвердил готовность",
}


def record_song_history(chat_id: int, title: str, status: str) -> None:
    song_history.setdefault(chat_id, []).append(
        {"title": title, "status": status, "timestamp": datetime.now(timezone.utc)}
    )


class Round:
    """Holds all state for a single in-progress song round. Only one active at a time."""

    def __init__(self, song: dict, filename: str, group_chat_id: int, participant_ids: list[int]):
        self.song = song
        self.filename = filename
        self.group_chat_id = group_chat_id
        self.participant_ids = participant_ids

        self.assignments: dict[int, list[int]] = {}      # participant_id -> ordered part indices
        self.current_pointer: dict[int, int] = {}         # participant_id -> pointer into assignments
        self.pending_take: dict[int, str] = {}             # participant_id -> latest unconfirmed file_id
        self.submissions: dict[int, dict] = {}              # part_index -> {"file_id", "user_id"}

        self.active = True
        self.job = None  # the scheduled finalize_round job, cancelled if the song completes early
        self.perk: Optional[str] = None  # random modifier for this round, if one was rolled

    def num_parts(self) -> int:
        return len(self.song["parts"])

    def all_confirmed(self) -> bool:
        return len(self.submissions) == self.num_parts()


current_round: Optional[Round] = None


class ReadinessCheck:
    """Holds state for the pre-round readiness poll that runs before a Round
    is created: every active (non-snoozed) registered participant is DM'd
    asking whether they're in for this song, and only whoever answers "Да"
    within READINESS_CHECK_SECONDS goes on to receive an assigned part."""

    def __init__(self, song: dict, filename: str, group_chat_id: int, candidate_ids: list[int]):
        self.song = song
        self.filename = filename
        self.group_chat_id = group_chat_id
        self.candidate_ids = candidate_ids

        self.responses: dict[int, bool] = {}  # participant_id -> True ("Да") / False ("Нет")

        self.active = True
        self.job = None  # the scheduled resolve_readiness_check job, cancelled if everyone answers early


current_readiness_check: Optional[ReadinessCheck] = None


def is_snoozed(chat_id: int, user_id: int) -> bool:
    """Whether the user is currently snoozed in this group. Lazily cleans up
    expired snoozes (memory + DB) as a side effect, so callers don't need to
    worry about stale entries lingering after the deadline passes.
    """
    until_ts = snoozed_until.get(chat_id, {}).get(user_id)
    if until_ts is None:
        return False
    if until_ts > int(datetime.now(timezone.utc).timestamp()):
        return True
    snoozed_until.get(chat_id, {}).pop(user_id, None)
    db_clear_snooze(chat_id, user_id)
    return False


def active_participant_ids(chat_id: int) -> list[int]:
    """Registered users for this group, excluding anyone currently snoozed."""
    return [
        uid for uid in registered_users.get(chat_id, {})
        if not is_snoozed(chat_id, uid)
    ]


DURATION_RE = re.compile(
    r"^(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$", re.IGNORECASE
)


def parse_duration(text: str) -> Optional[timedelta]:
    """Parses a duration like '30m', '2h', '3 days' into a timedelta. Returns
    None if the text doesn't match one of the supported minute/hour/day forms.
    """
    match = DURATION_RE.match(text.strip())
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith("m"):
        return timedelta(minutes=amount)
    if unit.startswith("h"):
        return timedelta(hours=amount)
    return timedelta(days=amount)


def resolve_registered_user(chat_id: int, ref: str) -> Optional[int]:
    """Resolves a command-argument user reference (`@username` or a raw numeric
    user id) to a user_id, but only among users already registered in this
    group. Telegram's Bot API has no general way to look up an arbitrary user
    by @username, so this only works for users already on file for this group
    from a prior /register -- replying to the target's own message (which
    carries the full User object) is the reliable alternative and doesn't
    have this limitation.
    """
    ref = ref.strip()
    group = registered_users.get(chat_id, {})
    if ref.startswith("@"):
        username = ref[1:].lower()
        for uid, info in group.items():
            if info.get("username") and info["username"].lower() == username:
                return uid
        return None
    if ref.lstrip("-").isdigit():
        uid = int(ref)
        return uid if uid in group else None
    return None

# ---------------------------------------------------------------------------
# Network resilience
# ---------------------------------------------------------------------------


async def call_with_retry(coro_factory, description, attempts=3, base_delay=2.0, max_flood_waits=3):
    """Retry a Telegram API call a few times on transient network errors
    (timeouts, dropped connections -- common on a flaky proxy/VPN path).
    Does NOT retry on permanent errors (bad request, forbidden, etc.) --
    those would just fail the same way every time.
    Returns None (and logs) if every attempt fails, rather than raising, so a
    caller isn't forced to add its own try/except for this specific failure mode.

    RetryAfter (flood control) is handled separately from the network-error
    retries above: Telegram tells us exactly how long to wait, so we sleep
    that long and retry without burning one of the normal `attempts` -- it's
    not a failure, just a rate limit. `max_flood_waits` still bounds it so a
    persistently flooded chat can't stall the caller forever.
    """
    last_exc = None
    flood_waits = 0
    attempt = 1
    while attempt <= attempts:
        try:
            return await coro_factory()
        except RetryAfter as e:
            flood_waits += 1
            if flood_waits > max_flood_waits:
                logger.error("%s hit flood control repeatedly, giving up: %s", description, e)
                return None
            wait = e.retry_after + 1
            logger.warning(
                "%s hit flood control, waiting %ss before retrying: %s", description, wait, e
            )
            await asyncio.sleep(wait)
        except (TimedOut, NetworkError) as e:
            last_exc = e
            logger.warning("%s failed (attempt %s/%s): %s", description, attempt, attempts, e)
            attempt += 1
            if attempt <= attempts:
                await asyncio.sleep(base_delay * (attempt - 1))
    logger.error("%s failed after %s attempts, giving up: %s", description, attempts, last_exc)
    return None


async def answer_callback_query_best_effort(query):
    """Acknowledge a callback query (clears the button's loading spinner).
    Purely cosmetic -- the actual confirm logic proceeds regardless of whether
    this succeeds. Deliberately makes exactly ONE attempt and never retries:
    Telegram's "query is too old" error (raised once the tap is no longer fresh)
    gets misclassified by the client library as a timeout-like error, so a naive
    retry loop would burn several seconds of backoff chasing a condition that
    retrying can never fix, delaying the real work below for no benefit.
    """
    try:
        await query.answer()
    except TelegramError as e:
        logger.warning("query.answer failed (non-fatal, continuing): %s", e)


async def is_group_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    """Whether `user_id` is currently an administrator or the creator of `chat_id`.
    Checked live against Telegram on every call (no caching), so a promotion or
    demotion takes effect immediately on the next command. Fails "closed" --
    any lookup error (network trouble that survives retries, or a permanent
    error like the bot no longer being in that chat) is treated as "not an
    admin" rather than silently granting access.
    """
    try:
        member = await call_with_retry(
            lambda: context.bot.get_chat_member(chat_id, user_id),
            description=f"admin check for user {user_id} in chat {chat_id}",
        )
    except TelegramError as e:
        # call_with_retry only retries transient network errors; a permanent
        # error (e.g. BadRequest, Forbidden) surfaces here instead of looping.
        logger.warning("Admin check failed for user %s in chat %s: %s", user_id, chat_id, e)
        return False
    if member is None:
        return False
    return member.status in ("administrator", "creator")

# ---------------------------------------------------------------------------
# Song loading
# ---------------------------------------------------------------------------


def load_random_song(exclude: Optional[set[str]] = None) -> tuple[dict, str, bool]:
    """Pick a random song file, skipping any filename in `exclude`.

    If every available song is excluded (the group has cycled through the
    whole catalog), the exclusion is ignored for this pick and any song may be
    chosen -- the caller is responsible for resetting its own tracking in that
    case. Returns (song, filename, exhausted) where `exhausted` signals that
    the exclusion set covered every file, so the caller should reset it before
    recording this pick.
    """
    files = sorted(SONGS_DIR.glob("*.json"))
    if not files:
        raise RuntimeError(f"No song files found in {SONGS_DIR}")
    exclude = exclude or set()
    available = [f for f in files if f.name not in exclude]
    exhausted = not available
    if exhausted:
        available = files
    path = random.choice(available)
    with open(path, "r", encoding="utf-8") as f:
        song = json.load(f)
    return song, path.name, exhausted


def maybe_pick_perk(probability: float = PERK_PROBABILITY) -> Optional[str]:
    """With the given probability, return one random line from the perks file
    (a modifier like "perform with a funny hat"). Returns None if the roll
    doesn't hit, or if the file is missing/empty.
    """
    if random.random() >= probability:
        return None
    if not PERKS_FILE.exists():
        logger.warning("Perks file not found at %s -- skipping perk selection.", PERKS_FILE)
        return None
    lines = [
        line.strip()
        for line in PERKS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not lines:
        return None
    return random.choice(lines)


def part_text(part: dict, sep: str = "\n") -> str:
    """Join a part's lines into a single string for display."""
    return sep.join(part["lines"])


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------


def assign_parts(song: dict, participant_ids: list[int]) -> dict[int, list[int]]:
    """Round-robin assign part indices to participants, in song order.
    If there are more participants than parts, the extras get nothing this round.
    """
    assignments: dict[int, list[int]] = {pid: [] for pid in participant_ids}
    for part_index in range(len(song["parts"])):
        owner = participant_ids[part_index % len(participant_ids)]
        assignments[owner].append(part_index)
    return assignments


# ---------------------------------------------------------------------------
# Message building (Russian user-facing text)
# ---------------------------------------------------------------------------


BLOCK_SEPARATOR = "\n\n\U0001F338\n\n"  # two blank lines + a flower emoji between each song block


def build_overview_message(song: dict, assigned_indices: list[int]) -> str:
    assigned_set = set(assigned_indices)
    blocks = []
    for i, part in enumerate(song["parts"]):
        joined = part_text(part, sep="\n")
        label = f"Часть {i + 1}"
        if i in assigned_set:
            blocks.append(f"\U0001F449 <b>{label}:\n{joined}</b>")
        else:
            blocks.append(f"{label}:\n{joined}")
    return f"<b>{song['title']}</b>\n\n" + BLOCK_SEPARATOR.join(blocks)


def build_prompt_text(song: dict, part_index: int, step: int, total: int) -> str:
    part = song["parts"][part_index]
    quoted = part_text(part, sep="\n")
    return (
        f"\U0001F3A5 Запишите часть {step}/{total}\n"
        f"\u00ab{quoted}\u00bb\n\n"
        f"Пришлите видеокружок с этой частью. Можно перезаписывать сколько угодно "
        f"раз \u2014 засчитывается только последний вариант. Нажмите «Подтвердить», "
        f"когда будете готовы."
    )


READY_KEYBOARD = InlineKeyboardMarkup(
    [[
        InlineKeyboardButton("✅ Да", callback_data="ready_yes"),
        InlineKeyboardButton("❌ Нет", callback_data="ready_no"),
    ]]
)


def build_readiness_text(song_title: str, group_title: str) -> str:
    minutes = READINESS_CHECK_SECONDS // 60
    return (
        f"\U0001F3A4 Скоро в группе «{html.escape(group_title)}» начнётся исполнение песни "
        f"«<b>{html.escape(song_title)}</b>».\n\n"
        f"Готовы принять в ней участие? Если не ответите в течение {minutes} минут, "
        "будем считать, что нет, и партии распределят без вас."
    )


CONFIRM_BUTTON = InlineKeyboardMarkup(
    [[InlineKeyboardButton("\u2705 Подтвердить и продолжить", callback_data="confirm_take")]]
)

# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Purely informational -- it no longer registers anyone by itself. Telegram
    # only lets a bot DM a user who has opened a private chat with it at least
    # once, so this doubles as the "unlock DMs" step before /register in a group.
    await call_with_retry(
        lambda: update.message.reply_text(
            "Привет! Чтобы участвовать в исполнении песен:\n"
            "1. Добавьте меня в группу, где будете выступать.\n"
            "2. Напишите /register в этой группе — так я запомню, что вы участвуете "
            "именно в её раунде (можно регистрироваться сразу в нескольких группах).\n"
            "3. Я буду присылать вам вашу партию сюда, в личные сообщения, когда "
            "в группе запустят /playsong.\n\n"
            "Чтобы перестать участвовать в конкретной группе, напишите там /unregister."
        ),
        description="start_command reply",
    )


async def register_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        await call_with_retry(
            lambda: update.message.reply_text(
                "Напишите /register в той группе, в которой хотите участвовать в исполнении песен."
            ),
            description="register_command: wrong chat type reply",
        )
        return

    user = update.effective_user
    chat_id = update.effective_chat.id
    display_name = user.first_name or user.username or str(user.id)

    db_add_registration(chat_id, user.id, display_name, user.username)
    registered_users.setdefault(chat_id, {})[user.id] = {"name": display_name, "username": user.username}

    await call_with_retry(
        lambda: update.message.reply_text(
            f"\u2705 {display_name}, вы зарегистрированы для исполнения песен в этой группе!"
        ),
        description="register_command: confirmation reply",
    )

    # Best-effort heads-up in DM. If this fails, the user most likely hasn't
    # opened a private chat with the bot yet -- warn them in the group, since
    # without that the bot won't be able to send them their part later.
    dm_ok = await call_with_retry(
        lambda: context.bot.send_message(
            chat_id=user.id,
            text=f"Вы зарегистрированы для исполнения песен в группе «{update.effective_chat.title}».",
        ),
        description=f"register_command: DM confirmation to {user.id}",
        attempts=1,
    )
    if dm_ok is None:
        await call_with_retry(
            lambda: update.message.reply_text(
                f"\u26a0\ufe0f {display_name}, похоже, я не могу написать вам в личные сообщения. "
                "Откройте со мной личный чат и нажмите /start, иначе я не смогу прислать вам вашу партию."
            ),
            description="register_command: DM-unreachable warning",
        )

    logger.info("Registered user %s (%s) for chat %s", user.id, display_name, chat_id)


async def unregister_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        await call_with_retry(
            lambda: update.message.reply_text(
                "Напишите /unregister в той группе, участие в которой хотите отменить."
            ),
            description="unregister_command: wrong chat type reply",
        )
        return

    user = update.effective_user
    chat_id = update.effective_chat.id

    was_registered = db_remove_registration(chat_id, user.id)
    registered_users.get(chat_id, {}).pop(user.id, None)
    snoozed_until.get(chat_id, {}).pop(user.id, None)

    if was_registered:
        text = "Вы отменили регистрацию на исполнение песен в этой группе."
        logger.info("Unregistered user %s from chat %s", user.id, chat_id)
    else:
        text = "Вы и так не были зарегистрированы в этой группе."

    await call_with_retry(
        lambda: update.message.reply_text(text),
        description="unregister_command reply",
    )


async def send_next_prompt(context: ContextTypes.DEFAULT_TYPE, participant_id: int):
    r = current_round
    pointer = r.current_pointer[participant_id]
    total = len(r.assignments[participant_id])
    if pointer >= total:
        return
    part_index = r.assignments[participant_id][pointer]
    text = build_prompt_text(r.song, part_index, pointer + 1, total)
    await call_with_retry(
        lambda: context.bot.send_message(chat_id=participant_id, text=text, parse_mode=ParseMode.HTML),
        description=f"prompt to {participant_id}",
    )


async def playsong_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_readiness_check

    if update.effective_chat.type not in ("group", "supergroup"):
        await call_with_retry(
            lambda: update.message.reply_text("Запустите /playsong в групповом чате."),
            description="playsong: wrong chat type reply",
        )
        return

    if not await is_group_admin(context, update.effective_chat.id, update.effective_user.id):
        await call_with_retry(
            lambda: update.message.reply_text(
                "Запускать /playsong могут только администраторы или создатель группы."
            ),
            description="playsong: not admin reply",
        )
        return

    if current_round is not None and current_round.active:
        await call_with_retry(
            lambda: update.message.reply_text("Раунд уже идёт."),
            description="playsong: already in progress reply",
        )
        return

    if current_readiness_check is not None and current_readiness_check.active:
        await call_with_retry(
            lambda: update.message.reply_text(
                "Идёт опрос готовности участников для предыдущего запуска /playsong. "
                "Дождитесь его завершения (или отмените его через /cancel)."
            ),
            description="playsong: readiness poll already in progress reply",
        )
        return

    group_registrations = registered_users.get(update.effective_chat.id, {})
    if not group_registrations:
        await call_with_retry(
            lambda: update.message.reply_text(
                "Пока никто не зарегистрировался в этой группе. Попросите участников "
                "написать здесь /register."
            ),
            description="playsong: no participants reply",
        )
        return

    chat_id = update.effective_chat.id
    candidate_ids = active_participant_ids(chat_id)
    if not candidate_ids:
        await call_with_retry(
            lambda: update.message.reply_text(
                "Все зарегистрированные участники этой группы сейчас в snooze. "
                "Проверьте /snooze или дождитесь его окончания."
            ),
            description="playsong: all snoozed reply",
        )
        return

    song, filename, exhausted = load_random_song(exclude=played_songs.get(chat_id))
    if exhausted:
        # Every song has already been played for this group -- start a fresh
        # rotation rather than refusing to play.
        played_songs[chat_id] = set()
        logger.info("Song rotation for chat %s exhausted; resetting.", chat_id)
    played_songs.setdefault(chat_id, set()).add(filename)

    rc = ReadinessCheck(song, filename, chat_id, candidate_ids)
    current_readiness_check = rc

    minutes = READINESS_CHECK_SECONDS // 60
    await call_with_retry(
        lambda: update.message.reply_text(
            f"\U0001F4E9 Спрашиваем участников, готовы ли они исполнить «{song['title']}»\u2026 "
            f"Ответ ожидается в течение {minutes} минут."
        ),
        description="playsong: readiness poll start reply",
    )

    readiness_text = build_readiness_text(song["title"], update.effective_chat.title)
    for pid in candidate_ids:
        try:
            await call_with_retry(
                lambda pid=pid: context.bot.send_message(
                    chat_id=pid,
                    text=readiness_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=READY_KEYBOARD,
                ),
                description=f"readiness poll DM to {pid}",
            )
        except Exception as e:
            # Same reasoning as the DM loop in start_round -- e.g. Forbidden if this
            # person blocked the bot. They just won't be able to answer, so they'll
            # be treated as "no response" once the poll resolves.
            logger.warning("Could not send readiness DM to %s: %s", pid, e)

    # The check is already created above, so even if every DM above failed, the
    # timer scheduled here must still run -- otherwise it would be stuck "active"
    # forever, permanently blocking any further /playsong in this chat.
    rc.job = context.job_queue.run_once(
        resolve_readiness_check, READINESS_CHECK_SECONDS, name="resolve_readiness"
    )


async def start_round(
    context: ContextTypes.DEFAULT_TYPE,
    song: dict,
    filename: str,
    chat_id: int,
    participant_ids: list[int],
) -> None:
    """Creates and kicks off the actual Round: assigns parts round-robin among
    `participant_ids` (those who confirmed readiness), announces it in the
    group, DMs each participant their part(s), and schedules finalize_round.
    """
    global current_round

    assignments = assign_parts(song, participant_ids)

    r = Round(song, filename, chat_id, participant_ids)
    r.assignments = assignments
    r.current_pointer = {pid: 0 for pid in participant_ids}
    r.perk = maybe_pick_perk()
    current_round = r

    # The round is already created above, so even if every send below fails
    # (e.g. a proxy outage), the timer scheduled at the end of this function
    # must still run -- otherwise the round would be stuck "active" forever.
    announcement = (
        f"\U0001F3B5 Начинаем раунд: <b>{song['title']}</b>\n"
        f"Проверьте личные сообщения \u2014 там ваша партия. "
        f"У вас есть {ROUND_DURATION_SECONDS // 60} минут."
    )
    if r.perk:
        announcement += f"\n\n\U0001F3B2 Задание: <b>{r.perk}</b>"
    await call_with_retry(
        lambda: context.bot.send_message(chat_id=chat_id, text=announcement, parse_mode=ParseMode.HTML),
        description="start_round: round start announcement",
    )

    for pid in participant_ids:
        if not assignments[pid]:
            continue  # more participants than parts; this one sits out this round
        try:
            await call_with_retry(
                lambda pid=pid: context.bot.send_message(
                    chat_id=pid,
                    text=build_overview_message(song, assignments[pid]),
                    parse_mode=ParseMode.HTML,
                ),
                description=f"overview DM to {pid}",
            )
            await send_next_prompt(context, pid)
        except Exception as e:
            # Non-network failures (e.g. Forbidden if this person blocked the bot)
            # shouldn't stop the rest of the group from being DM'd, and the timer
            # below must still get scheduled regardless.
            logger.warning("Could not DM participant %s: %s", pid, e)

    r.job = context.job_queue.run_once(finalize_round, ROUND_DURATION_SECONDS, name="finalize_round")


async def resolve_readiness_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Called once READINESS_CHECK_SECONDS elapses, or earlier if every
    candidate has already answered (see readiness_callback). Starts the real
    round with whoever said "Да"; if nobody did, the round never happens.
    """
    global current_readiness_check
    rc = current_readiness_check
    if rc is None or not rc.active:
        return
    rc.active = False
    current_readiness_check = None

    ready_ids = [pid for pid in rc.candidate_ids if rc.responses.get(pid) is True]

    if not ready_ids:
        record_song_history(rc.group_chat_id, rc.song["title"], "no_participants")
        text = (
            f"\U0001F614 Никто не подтвердил готовность исполнить «{rc.song['title']}». "
            "Раунд отменён \u2014 запустите /playsong ещё раз, когда участники будут готовы."
        )
        await call_with_retry(
            lambda: context.bot.send_message(chat_id=rc.group_chat_id, text=text),
            description="resolve_readiness_check: nobody ready group announcement",
        )
        return

    await start_round(context, rc.song, rc.filename, rc.group_chat_id, ready_ids)


async def readiness_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rc = current_readiness_check
    query = update.callback_query
    user_id = query.from_user.id
    await answer_callback_query_best_effort(query)

    if rc is None or not rc.active or user_id not in rc.candidate_ids:
        await call_with_retry(
            lambda: query.edit_message_text("Этот опрос готовности уже завершён."),
            description="readiness_callback: expired reply",
        )
        return

    is_ready = query.data == "ready_yes"
    rc.responses[user_id] = is_ready

    if is_ready:
        ack = f"\u2705 Отлично, вы участвуете в исполнении «{rc.song['title']}»!"
    else:
        ack = "Хорошо, в этот раз пропустим. Увидимся в следующий раз!"
    await call_with_retry(
        lambda: query.edit_message_text(ack),
        description="readiness_callback: ack reply",
    )

    if len(rc.responses) >= len(rc.candidate_ids):
        # Everyone's answered -- no need to wait out the rest of the timer.
        if rc.job is not None:
            rc.job.schedule_removal()
        await resolve_readiness_check(context)


async def video_note_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    r = current_round
    user_id = update.effective_user.id

    if r is None or not r.active:
        await call_with_retry(
            lambda: update.message.reply_text(
                "Сейчас нет активного раунда. Дождитесь /playsong в группе."
            ),
            description="video_note_handler: no active round reply",
        )
        return

    if user_id not in r.assignments or not r.assignments[user_id]:
        # Most common cause: this person registered (/start) after the round already
        # started, so they were never included in the part assignments.
        await call_with_retry(
            lambda: update.message.reply_text(
                "Вы не участвуете в текущем раунде (возможно, вы зарегистрировались уже "
                "после того, как раунд начался). Дождитесь следующего /playsong."
            ),
            description="video_note_handler: not in round reply",
        )
        return

    pointer = r.current_pointer.get(user_id, 0)
    total = len(r.assignments[user_id])
    if pointer >= total:
        await call_with_retry(
            lambda: update.message.reply_text("Вы уже отправили все свои части в этом раунде."),
            description="video_note_handler: already done reply",
        )
        return

    file_id = update.message.video_note.file_id
    r.pending_take[user_id] = file_id
    await call_with_retry(
        lambda: update.message.reply_text(
            "Принято! Пришлите другой дубль, чтобы заменить, или нажмите «Подтвердить», когда будете готовы.",
            reply_markup=CONFIRM_BUTTON,
        ),
        description="video_note_handler: confirm button reply",
    )


async def wrong_video_type_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Catches a regular video (gallery upload, forwarded clip, etc.) sent instead of
    # a genuine video circle. filters.VIDEO_NOTE would silently ignore this, which is
    # confusing -- tell the participant what went wrong instead.
    r = current_round
    if r is not None and r.active and update.effective_user.id in r.assignments:
        await call_with_retry(
            lambda: update.message.reply_text(
                "Это обычное видео, а не видеокружок \u2014 запишите именно круглое "
                "видеосообщение (в Telegram: значок камеры с кружком в поле ввода) "
                "и пришлите его сюда."
            ),
            description="wrong_video_type_handler reply",
        )


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    r = current_round
    query = update.callback_query
    user_id = query.from_user.id
    await answer_callback_query_best_effort(query)

    if r is None or not r.active or user_id not in r.assignments:
        await call_with_retry(
            lambda: query.edit_message_text("Этот раунд уже завершён."),
            description="confirm_callback: round over reply",
        )
        return

    take = r.pending_take.get(user_id)
    if take is None:
        await call_with_retry(
            lambda: query.edit_message_text("Сначала пришлите видеокружок, потом подтвердите."),
            description="confirm_callback: no take reply",
        )
        return

    pointer = r.current_pointer[user_id]
    part_index = r.assignments[user_id][pointer]
    r.submissions[part_index] = {"file_id": take, "user_id": user_id}
    del r.pending_take[user_id]
    r.current_pointer[user_id] += 1

    await call_with_retry(
        lambda: query.edit_message_text(f"\u2705 Зафиксировано: часть {part_index + 1}"),
        description="confirm_callback: locked-in reply",
    )

    song_complete = r.all_confirmed()

    if r.current_pointer[user_id] < len(r.assignments[user_id]):
        await send_next_prompt(context, user_id)
    else:
        await call_with_retry(
            lambda: context.bot.send_message(
                chat_id=user_id, text="Вы всё сделали! Ждём остальных участников группы."
            ),
            description="confirm_callback: all done reply",
        )
        if not song_complete:
            # If the song just became 100% complete, skip this -- finalize_round
            # (triggered below) posts its own "song ready" message instead, and a
            # "95%... 100%!" pair right before it would just be noise.
            percent = round(100 * len(r.submissions) / r.num_parts())
            await call_with_retry(
                lambda: context.bot.send_message(
                    chat_id=r.group_chat_id,
                    text=f"Песня {r.song['title']} исполнена на {percent} %",
                ),
                description="confirm_callback: group progress update",
            )

    if song_complete:
        # Every part is in -- publish now rather than waiting out the rest of the timer.
        if r.job is not None:
            r.job.schedule_removal()
        await finalize_round(context)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Immediately cancels whatever's in progress -- an active round, or the
    readiness poll that precedes one -- and notifies the group plus every
    participant who had a part assigned or was asked about readiness. Only an
    administrator or the creator of the relevant group may do this. Can be
    invoked either from that group directly, or via DM (useful if the group
    chat itself is unreachable) -- either way, permission is checked against
    the round's/poll's group, not the chat the command was typed in.
    """
    global current_round, current_readiness_check
    r = current_round
    rc = current_readiness_check

    round_active = r is not None and r.active
    readiness_active = rc is not None and rc.active

    if not round_active and not readiness_active:
        await call_with_retry(
            lambda: update.message.reply_text("Сейчас нет активного раунда для отмены."),
            description="cancel_command: no active round reply",
        )
        return

    # A round is only ever created once its readiness poll has resolved, so
    # the two are never simultaneously active -- at most one of these is set.
    group_chat_id = r.group_chat_id if round_active else rc.group_chat_id

    chat = update.effective_chat
    user_id = update.effective_user.id
    called_from_group = chat.type in ("group", "supergroup") and chat.id == group_chat_id
    called_from_dm = chat.type == "private"

    if not (called_from_group or called_from_dm):
        await call_with_retry(
            lambda: update.message.reply_text(
                "Эту команду нужно вызвать в группе, где идёт раунд, либо в личных сообщениях."
            ),
            description="cancel_command: wrong context reply",
        )
        return

    if not await is_group_admin(context, group_chat_id, user_id):
        await call_with_retry(
            lambda: update.message.reply_text(
                "Отменять исполнение песни могут только администраторы или создатель группы."
            ),
            description="cancel_command: not admin reply",
        )
        return

    if readiness_active:
        rc.active = False
        if rc.job is not None:
            rc.job.schedule_removal()

        record_song_history(rc.group_chat_id, rc.song["title"], "cancelled")

        text = f"\u274c Опрос готовности для «{rc.song['title']}» отменён администратором."
        await call_with_retry(
            lambda: context.bot.send_message(chat_id=rc.group_chat_id, text=text),
            description="cancel_command: readiness poll group announcement",
        )
        for pid in rc.candidate_ids:
            await call_with_retry(
                lambda pid=pid: context.bot.send_message(chat_id=pid, text=text),
                description=f"cancel_command: readiness poll DM to {pid}",
            )

        logger.info(
            "Readiness poll for chat %s (song file %s) cancelled by user %s",
            rc.group_chat_id, rc.filename, user_id,
        )
        current_readiness_check = None
        return

    r.active = False
    if r.job is not None:
        r.job.schedule_removal()

    record_song_history(r.group_chat_id, r.song["title"], "cancelled")

    song_title = r.song["title"]
    text = f"\u274c Исполнение песни «{song_title}» отменено."

    await call_with_retry(
        lambda: context.bot.send_message(chat_id=r.group_chat_id, text=text),
        description="cancel_command: group announcement",
    )
    for pid in r.participant_ids:
        if not r.assignments.get(pid):
            continue  # sat out this round (more participants than parts); nothing to tell them
        await call_with_retry(
            lambda pid=pid: context.bot.send_message(chat_id=pid, text=text),
            description=f"cancel_command: DM to {pid}",
        )

    logger.info(
        "Round for chat %s (song file %s) cancelled by user %s", r.group_chat_id, r.filename, user_id
    )
    current_round = None


def _extract_target(update: Update, args: list[str]) -> tuple[Optional[int], Optional[str]]:
    """Shared target-resolution for the admin commands below. Returns
    (target_user_id, remaining_args_error) where the second value, if not
    None, is a ready-to-send Russian error message explaining why no target
    could be resolved (missing/bad reference) -- args itself is NOT consumed
    here since duration parsing differs between /snooze and /unregister_user.
    Prefers replying to the target's own message (always works); falls back
    to the first arg being '@username' or a numeric id among users already
    registered in this group.
    """
    chat_id = update.effective_chat.id
    reply_to = update.message.reply_to_message
    if reply_to is not None and reply_to.from_user is not None:
        return reply_to.from_user.id, None
    if not args:
        return None, None
    target_id = resolve_registered_user(chat_id, args[0])
    if target_id is None:
        return None, (
            f"Не нашёл среди зарегистрированных в этой группе пользователя {args[0]}. "
            "Либо ответьте этой командой на сообщение нужного участника."
        )
    return target_id, None


async def snooze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only. Temporarily excludes a still-registered user from this
    group's rounds for a given duration (they keep their registration and
    don't need to /register again once it expires). Usage:
      Reply to their message:  /snooze 2h
      Or by reference:         /snooze @username 2h   /snooze 123456789 30m
    Duration units: m/min/minutes, h/hour/hours, d/day/days.
    """
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await call_with_retry(
            lambda: update.message.reply_text("Запустите /snooze в групповом чате."),
            description="snooze_command: wrong chat type reply",
        )
        return

    if not await is_group_admin(context, chat.id, update.effective_user.id):
        await call_with_retry(
            lambda: update.message.reply_text(
                "Использовать /snooze могут только администраторы или создатель группы."
            ),
            description="snooze_command: not admin reply",
        )
        return

    chat_id = chat.id
    args = context.args or []
    reply_to = update.message.reply_to_message

    if reply_to is not None and reply_to.from_user is not None:
        if len(args) != 1:
            await call_with_retry(
                lambda: update.message.reply_text(
                    "Ответьте на сообщение участника и укажите длительность, например: /snooze 2h"
                ),
                description="snooze_command: usage (reply form) reply",
            )
            return
        target_id, error = reply_to.from_user.id, None
        duration_text = args[0]
    else:
        if len(args) != 2:
            await call_with_retry(
                lambda: update.message.reply_text(
                    "Использование: /snooze @username 2h\n"
                    "(или ответьте на сообщение участника командой /snooze 2h)\n"
                    "Единицы: m/min (минуты), h/hour (часы), d/day (дни)."
                ),
                description="snooze_command: usage reply",
            )
            return
        target_id, error = _extract_target(update, args[:1])
        duration_text = args[1]

    if error:
        await call_with_retry(lambda: update.message.reply_text(error), description="snooze_command: target not found reply")
        return

    if target_id not in registered_users.get(chat_id, {}):
        await call_with_retry(
            lambda: update.message.reply_text("Этот пользователь не зарегистрирован в этой группе."),
            description="snooze_command: not registered reply",
        )
        return

    duration = parse_duration(duration_text)
    if duration is None:
        await call_with_retry(
            lambda: update.message.reply_text(
                f"Не понимаю длительность «{duration_text}». Примеры: 30m, 2h, 3d."
            ),
            description="snooze_command: bad duration reply",
        )
        return

    until = datetime.now(timezone.utc) + duration
    until_ts = int(until.timestamp())
    snoozed_until.setdefault(chat_id, {})[target_id] = until_ts
    db_set_snooze(chat_id, target_id, until_ts)

    target_name = registered_users[chat_id][target_id]["name"]
    until_str = until.strftime("%d.%m.%Y %H:%M UTC")

    await call_with_retry(
        lambda: update.message.reply_text(
            f"\U0001F634 {target_name} не будет участвовать в раундах этой группы до {until_str}."
        ),
        description="snooze_command: confirmation reply",
    )
    await call_with_retry(
        lambda: context.bot.send_message(
            chat_id=target_id,
            text=(
                f"Администратор группы «{chat.title}» приостановил ваше участие в "
                f"исполнении песен до {until_str}. Регистрация сохраняется — участие "
                "возобновится автоматически."
            ),
        ),
        description=f"snooze_command: DM to {target_id}",
        attempts=1,
    )

    logger.info(
        "User %s snoozed in chat %s until %s (%s) by admin %s",
        target_id, chat_id, until_str, duration_text, update.effective_user.id,
    )


async def unsnooze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only. Lifts a snooze early. Usage:
      Reply to their message:  /unsnooze
      Or by reference:         /unsnooze @username   /unsnooze 123456789
    """
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await call_with_retry(
            lambda: update.message.reply_text("Запустите /unsnooze в групповом чате."),
            description="unsnooze_command: wrong chat type reply",
        )
        return

    if not await is_group_admin(context, chat.id, update.effective_user.id):
        await call_with_retry(
            lambda: update.message.reply_text(
                "Использовать /unsnooze могут только администраторы или создатель группы."
            ),
            description="unsnooze_command: not admin reply",
        )
        return

    chat_id = chat.id
    args = context.args or []
    target_id, error = _extract_target(update, args)
    if error:
        await call_with_retry(lambda: update.message.reply_text(error), description="unsnooze_command: target not found reply")
        return
    if target_id is None:
        await call_with_retry(
            lambda: update.message.reply_text(
                "Использование: /unsnooze @username (или ответьте на сообщение участника командой /unsnooze)"
            ),
            description="unsnooze_command: usage reply",
        )
        return

    was_snoozed = snoozed_until.get(chat_id, {}).pop(target_id, None) is not None
    was_snoozed = db_clear_snooze(chat_id, target_id) or was_snoozed

    if was_snoozed:
        name = registered_users.get(chat_id, {}).get(target_id, {}).get("name", str(target_id))
        text = f"{name} снова может участвовать в раундах этой группы."
        logger.info("Snooze cleared for user %s in chat %s by admin %s", target_id, chat_id, update.effective_user.id)
    else:
        text = "Этот пользователь сейчас не в snooze."

    await call_with_retry(lambda: update.message.reply_text(text), description="unsnooze_command: confirmation reply")


async def unregister_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only. Completely removes another user's registration for this
    group (unlike /snooze, this is permanent -- they'd need to /register
    again to come back). Usage:
      Reply to their message:  /unregister_user
      Or by reference:         /unregister_user @username   /unregister_user 123456789
    """
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await call_with_retry(
            lambda: update.message.reply_text("Запустите /unregister_user в групповом чате."),
            description="unregister_user_command: wrong chat type reply",
        )
        return

    if not await is_group_admin(context, chat.id, update.effective_user.id):
        await call_with_retry(
            lambda: update.message.reply_text(
                "Использовать /unregister_user могут только администраторы или создатель группы."
            ),
            description="unregister_user_command: not admin reply",
        )
        return

    chat_id = chat.id
    args = context.args or []
    target_id, error = _extract_target(update, args)
    if error:
        await call_with_retry(lambda: update.message.reply_text(error), description="unregister_user_command: target not found reply")
        return
    if target_id is None:
        await call_with_retry(
            lambda: update.message.reply_text(
                "Использование: /unregister_user @username\n"
                "(или ответьте на сообщение участника командой /unregister_user)"
            ),
            description="unregister_user_command: usage reply",
        )
        return

    target_name = registered_users.get(chat_id, {}).get(target_id, {}).get("name", str(target_id))

    was_registered = db_remove_registration(chat_id, target_id)  # also clears any snooze row
    registered_users.get(chat_id, {}).pop(target_id, None)
    snoozed_until.get(chat_id, {}).pop(target_id, None)

    if was_registered:
        await call_with_retry(
            lambda: update.message.reply_text(
                f"Регистрация пользователя {target_name} в этой группе полностью отменена "
                "администратором. Чтобы участвовать снова, нужно написать /register."
            ),
            description="unregister_user_command: confirmation reply",
        )
        await call_with_retry(
            lambda: context.bot.send_message(
                chat_id=target_id,
                text=(
                    f"Администратор группы «{chat.title}» отменил вашу регистрацию на "
                    "исполнение песен. Напишите /register в группе, если захотите участвовать снова."
                ),
            ),
            description=f"unregister_user_command: DM to {target_id}",
            attempts=1,
        )
        logger.info("User %s force-unregistered from chat %s by admin %s", target_id, chat_id, update.effective_user.id)
    else:
        await call_with_retry(
            lambda: update.message.reply_text(f"{target_name} и так не был(а) зарегистрирован(а) в этой группе."),
            description="unregister_user_command: not registered reply",
        )


async def concat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only. Turns this group's post-round combined video on or off --
    when on, finalize_round posts one extra video (all of this song's clips
    concatenated in song order) after the individual circles, once a round
    finishes successfully. Off by default for every group until an admin
    runs this. Usage:
      /concat on
      /concat off
    """
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await call_with_retry(
            lambda: update.message.reply_text("Запустите /concat в групповом чате."),
            description="concat_command: wrong chat type reply",
        )
        return

    if not await is_group_admin(context, chat.id, update.effective_user.id):
        await call_with_retry(
            lambda: update.message.reply_text(
                "Использовать /concat могут только администраторы или создатель группы."
            ),
            description="concat_command: not admin reply",
        )
        return

    args = context.args or []
    if len(args) != 1 or args[0].lower() not in ("on", "off"):
        await call_with_retry(
            lambda: update.message.reply_text("Использование: /concat on  или  /concat off"),
            description="concat_command: usage reply",
        )
        return

    enabled = args[0].lower() == "on"
    chat_id = chat.id
    concat_enabled[chat_id] = enabled
    db_set_concat_enabled(chat_id, enabled)

    if enabled:
        text = (
            "✅ Склейка общего видео из всех кружков включена. После каждой "
            "успешно исполненной песни в группу дополнительно придёт одно общее видео."
        )
        if not ffmpeg_available():
            text += (
                "\n\n⚠️ На сервере бота не найден ffmpeg, поэтому склейка сейчас "
                "не сработает (ошибка уйдёт только в лог, группу это не потревожит). "
                "Настройка сохранена и заработает, как только ffmpeg установят."
            )
    else:
        text = "❌ Склейка общего видео выключена. Будут приходить только отдельные кружки."

    await call_with_retry(
        lambda: update.message.reply_text(text),
        description="concat_command: confirmation reply",
    )

    logger.info(
        "Concatenated-video setting for chat %s set to %s by admin %s",
        chat_id, enabled, update.effective_user.id,
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Open to everyone (not admin-only) -- a friendly, emoji-led snapshot of
    this group's roster (who's registered, who's snoozed) and every song
    played since the bot process last started (see song_history above for why
    that history doesn't survive a restart).
    """
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await call_with_retry(
            lambda: update.message.reply_text("Запустите /stats в групповом чате."),
            description="stats_command: wrong chat type reply",
        )
        return

    chat_id = chat.id
    group_registrations = registered_users.get(chat_id, {})

    participant_lines = []
    active_count = 0
    for uid, info in sorted(group_registrations.items(), key=lambda kv: kv[1]["name"].lower()):
        name = info["name"]
        if is_snoozed(chat_id, uid):  # also lazily clears expired snoozes as a side effect
            until_ts = snoozed_until[chat_id][uid]
            until_str = datetime.fromtimestamp(until_ts, tz=timezone.utc).strftime("%d.%m %H:%M UTC")
            participant_lines.append(f"\u2022 {name} \U0001F634 <i>(на паузе до {until_str})</i>")
        else:
            participant_lines.append(f"\u2022 {name} \u2705")
            active_count += 1

    total = len(group_registrations)
    snoozed_count = total - active_count

    lines = [f"\U0001F4CA <b>Статистика группы «{chat.title}»</b>", ""]
    lines.append(f"\U0001F465 Зарегистрировано участников: <b>{total}</b>")
    lines.append(f"\u2705 Готовы выступать прямо сейчас: <b>{active_count}</b>")
    lines.append(f"\U0001F634 На паузе (snooze): <b>{snoozed_count}</b>")
    lines.append("")

    if participant_lines:
        lines.append("<b>\U0001F465 Участники:</b>")
        lines.extend(participant_lines)
    else:
        lines.append("Пока никто не зарегистрировался. Напишите /register, чтобы начать! \U0001F3A4")

    lines.append("")
    lines.append("\U0001F3B5 <b>Песни с момента последнего запуска бота</b> <i>(сначала свежие)</i>:")
    history = song_history.get(chat_id, [])
    if history:
        for i, entry in enumerate(reversed(history), start=1):
            emoji = SONG_STATUS_EMOJI.get(entry["status"], "\u2753")
            status = SONG_STATUS_TEXT.get(entry["status"], entry["status"])
            when = entry["timestamp"].strftime("%d.%m %H:%M UTC")
            lines.append(f"{i}. {emoji} «{entry['title']}» \u2014 {status} <i>({when})</i>")
    else:
        lines.append("Пока ни одна песня не была сыграна. Запустите /playsong! \U0001F3A7")

    text = "\n".join(lines)
    await call_with_retry(
        lambda: update.message.reply_text(text, parse_mode=ParseMode.HTML),
        description="stats_command reply",
    )


async def finalize_round(context: ContextTypes.DEFAULT_TYPE):
    global current_round
    r = current_round
    if r is None or not r.active:
        return
    r.active = False

    if r.all_confirmed():
        record_song_history(r.group_chat_id, r.song["title"], "success")
        intro = f"\U0001F3AC Песня готова: {r.song['title']}"
        if r.perk:
            intro += f"\n\U0001F3B2 Задание: {r.perk}"
        await call_with_retry(
            lambda: context.bot.send_message(chat_id=r.group_chat_id, text=intro),
            description="finalize_round: intro message",
        )
        for part_index in range(r.num_parts()):
            submission = r.submissions[part_index]
            if part_index > 0:
                # Small pacing gap so a long song's back-to-back circles don't
                # trip Telegram's per-chat flood control (see the RetryAfter
                # handling in call_with_retry).
                await asyncio.sleep(VIDEO_NOTE_SEND_DELAY_SECONDS)
            # Keep going even if this one clip ultimately fails to send after
            # retries -- one dropped connection shouldn't cancel the rest of
            # the performance. No caption -- who's performing is obvious from
            # the person visible in the circle.
            await call_with_retry(
                lambda submission=submission: context.bot.send_video_note(
                    chat_id=r.group_chat_id, video_note=submission["file_id"]
                ),
                description=f"finalize_round: video note for part {part_index}",
            )

        if concat_enabled.get(r.group_chat_id, False):
            await send_concatenated_video(context, r)
    else:
        record_song_history(r.group_chat_id, r.song["title"], "incomplete")
        missing = r.num_parts() - len(r.submissions)
        failure_text = (
            f"\u274c Песня {r.filename} не была исполнена полностью "
            f"(не хватает частей: {missing}). Введите /playsong ещё раз, чтобы выбрать новую песню."
        )
        await call_with_retry(
            lambda: context.bot.send_message(chat_id=r.group_chat_id, text=failure_text),
            description="finalize_round: incomplete-song group announcement",
        )
        for pid in r.participant_ids:
            await call_with_retry(
                lambda pid=pid: context.bot.send_message(chat_id=pid, text=failure_text),
                description=f"finalize_round: incomplete-song DM to {pid}",
            )

    current_round = None


async def send_concatenated_video(context: ContextTypes.DEFAULT_TYPE, r: Round) -> None:
    """Best-effort: builds one combined video from this round's clips (song
    order) and posts it to the group in addition to the individual circles
    finalize_round already sent above. Never touches the group on failure --
    only the log -- since the individual clips are the guaranteed
    deliverable and this is a bonus on top of them.
    """
    file_ids = [r.submissions[i]["file_id"] for i in range(r.num_parts())]
    with tempfile.TemporaryDirectory(prefix="musicbeads_concat_") as tmp_dir:
        try:
            combined_path = await build_concatenated_video(context.bot, file_ids, Path(tmp_dir))
            with open(combined_path, "rb") as f:
                video_bytes = f.read()
        except VideoConcatError as e:
            logger.error(
                "Video concatenation failed for chat %s (song %s): %s",
                r.group_chat_id, r.song["title"], e,
            )
            return
        except Exception:
            logger.exception(
                "Unexpected error building concatenated video for chat %s (song %s)",
                r.group_chat_id, r.song["title"],
            )
            return

    # video_bytes (not a file handle) so a retry inside call_with_retry re-sends
    # the same bytes rather than re-reading an already-exhausted file cursor.
    await call_with_retry(
        lambda: context.bot.send_video(
            chat_id=r.group_chat_id,
            video=video_bytes,
            caption=f"\U0001F3AC «{r.song['title']}» — общая запись",
        ),
        description="finalize_round: combined video upload",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    # Catches anything an individual handler didn't handle itself (e.g. a network
    # timeout on a call not wrapped in try/except). Logs it instead of letting it
    # silently vanish; the bot keeps running either way -- PTB isolates exceptions
    # per update.
    if isinstance(context.error, (NetworkError, TimedOut)):
        # Transient connectivity blip (e.g. proxy/connection reset during getUpdates
        # long polling). PTB's polling loop already retries these on its own, so this
        # isn't a bug -- just note it without the alarming ERROR+traceback noise.
        logger.warning("Network hiccup while processing update %s: %s", update, context.error)
        return
    logger.error("Unhandled exception while processing update %s", update, exc_info=context.error)


async def announce_startup(app: Application) -> None:
    """Post-init hook: let every group the bot knows about (i.e. has at least
    one registration) know the bot process just (re)started, before polling
    begins. Best-effort per chat -- one chat failing (bot kicked, etc.)
    shouldn't stop the announcement from reaching the others.
    """
    text = "\U0001F3B5 Бот перезапущен и снова на связи!"
    for chat_id in registered_users:
        await call_with_retry(
            lambda chat_id=chat_id: app.bot.send_message(chat_id=chat_id, text=text),
            description=f"announce_startup: chat {chat_id}",
        )


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Set the SONG_BOT_TOKEN environment variable.")

    init_db()
    global registered_users, snoozed_until, concat_enabled
    registered_users = load_registrations()
    snoozed_until = load_snoozes()
    concat_enabled = load_concat_settings()
    total = sum(len(users) for users in registered_users.values())
    logger.info(
        "Loaded %s registration(s) across %s group(s) from %s",
        total, len(registered_users), DB_FILE,
    )
    snoozed_total = sum(len(users) for users in snoozed_until.values())
    if snoozed_total:
        logger.info("Loaded %s active snooze(s) across %s group(s)", snoozed_total, len(snoozed_until))
    concat_total = sum(concat_enabled.values())
    if concat_total:
        logger.info("Loaded concatenated-video setting: enabled for %s group(s)", concat_total)
    if not ffmpeg_available():
        logger.warning(
            "ffmpeg not found on PATH -- any group that enables /concat will have its "
            "combined-video step fail (logged, not shown to the group) until ffmpeg is installed."
        )

    # getUpdates (long polling) holds a connection open for its whole poll interval.
    # If it shared a pool with outgoing calls (sendMessage, sendVideoNote, etc.), those
    # calls could queue behind it and hit a pool timeout -- give it its own small pool,
    # and a bigger pool for everything else so several sends don't block each other.
    request = HTTPXRequest(
        connection_pool_size=8, connect_timeout=20.0, read_timeout=20.0,
        write_timeout=20.0, pool_timeout=20.0,
    )
    get_updates_request = HTTPXRequest(
        connection_pool_size=1, connect_timeout=20.0, read_timeout=20.0,
        write_timeout=20.0, pool_timeout=20.0,
    )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .get_updates_request(get_updates_request)
        .post_init(announce_startup)
        .build()
    )
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("register", register_command))
    app.add_handler(CommandHandler("unregister", unregister_command))
    app.add_handler(CommandHandler("playsong", playsong_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("snooze", snooze_command))
    app.add_handler(CommandHandler("unsnooze", unsnooze_command))
    app.add_handler(CommandHandler("unregister_user", unregister_user_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("concat", concat_command))
    app.add_handler(MessageHandler(filters.VIDEO_NOTE, video_note_handler))
    app.add_handler(MessageHandler(filters.VIDEO, wrong_video_type_handler))
    app.add_handler(CallbackQueryHandler(confirm_callback, pattern="^confirm_take$"))
    app.add_handler(CallbackQueryHandler(readiness_callback, pattern="^ready_(yes|no)$"))
    app.add_error_handler(error_handler)

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()