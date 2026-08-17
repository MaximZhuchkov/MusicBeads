"""
Bare-bones song relay bot (Russian UI, multi-line song parts).

Flow:
  1. Participants type /register in a group chat to register as a performer for
     THAT group (one user can be registered in multiple groups). /unregister
     removes them from that group's roster. Registrations are persisted to a
     local SQLite database, so they survive bot restarts.
  2. Someone types /playsong in the group -> bot picks a random song, round-robin
     assigns parts to that group's registered participants, and DMs each their
     part(s).
  3. If a participant has multiple parts, they're prompted one at a time. They can
     send a video circle repeatedly (each overwrites the last) and tap "Confirm"
     when happy, which locks in that part and advances to the next one.
  4. After ROUND_DURATION_SECONDS, the bot checks whether every part was confirmed.
     If yes, it posts each video circle to the group in song order. If no, it
     announces the song as incomplete and the round ends.
  5. At any point during an active round, /cancel (typed in the group, or in DM
     by one of that round's participants) immediately ends it and notifies both
     the group and every participant who had a part.

Song selection avoids repeats: each group won't be given the same song twice in
a row across separate /playsong runs until every song in songs/ has come up for
that group, at which point the rotation for that group resets. This tracking is
in-memory only -- it resets when the bot restarts, so a restart can hand out a
song that was already played in the previous run.

Song JSON schema (see songs/example_song.json):
  {
    "title": "...",
    "parts": [
      { "id": "verse1", "label": "Куплет 1", "lines": ["line one", "line two"] },
      ...
    ]
  }
  Each part's "lines" is a list of strings so a single component can span
  multiple lines of lyrics.

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
    /unregister remain open to everyone.
  - Per-group "already played" song tracking is in-memory only (see module
    docstring above); it is deliberately NOT persisted to the DB.
"""

import asyncio
import json
import logging
import os
import random
import sqlite3
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import NetworkError, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

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
ROUND_DURATION_SECONDS = int(os.environ.get("ROUND_DURATION_SECONDS", 300))
PERK_PROBABILITY = float(os.environ.get("PERK_PROBABILITY", 0.5))

# ---------------------------------------------------------------------------
# Persistence (SQLite-backed registrations)
# ---------------------------------------------------------------------------
#
# A registration is a (group_chat_id, user_id) pair -- a user can be registered
# in several groups at once, and each group has its own independent roster.
# Everything else (rounds, assignments, submissions) stays in-memory as before.


def init_db() -> None:
    """Create the registrations table if it doesn't exist yet. Safe to call every startup."""
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS registrations (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )
        conn.commit()


def load_registrations() -> dict[int, dict[int, str]]:
    """Load all persisted registrations into the in-memory shape used at runtime:
    chat_id -> {user_id: display_name}.
    """
    result: dict[int, dict[int, str]] = {}
    with sqlite3.connect(DB_FILE) as conn:
        for chat_id, user_id, display_name in conn.execute(
            "SELECT chat_id, user_id, display_name FROM registrations"
        ):
            result.setdefault(chat_id, {})[user_id] = display_name
    return result


def db_add_registration(chat_id: int, user_id: int, display_name: str) -> None:
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "INSERT INTO registrations (chat_id, user_id, display_name) VALUES (?, ?, ?) "
            "ON CONFLICT (chat_id, user_id) DO UPDATE SET display_name = excluded.display_name",
            (chat_id, user_id, display_name),
        )
        conn.commit()


def db_remove_registration(chat_id: int, user_id: int) -> bool:
    """Returns True if a row was actually deleted (i.e. the user was registered)."""
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute(
            "DELETE FROM registrations WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        )
        conn.commit()
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

# chat_id -> {user_id: display_name}. Mirrors the `registrations` DB table and is
# populated from it at startup by load_registrations(); every mutation below also
# writes through to the DB so the two stay in sync.
registered_users: dict[int, dict[int, str]] = {}

# chat_id -> set of song filenames already picked for that group. Intentionally
# NOT persisted -- starts empty every time the bot restarts (see module docstring).
played_songs: dict[int, set[str]] = {}


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

# ---------------------------------------------------------------------------
# Network resilience
# ---------------------------------------------------------------------------


async def call_with_retry(coro_factory, description, attempts=3, base_delay=2.0):
    """Retry a Telegram API call a few times on transient network errors
    (timeouts, dropped connections -- common on a flaky proxy/VPN path).
    Does NOT retry on permanent errors (bad request, forbidden, etc.) --
    those would just fail the same way every time.
    Returns None (and logs) if every attempt fails, rather than raising, so a
    caller isn't forced to add its own try/except for this specific failure mode.
    """
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return await coro_factory()
        except (TimedOut, NetworkError) as e:
            last_exc = e
            logger.warning("%s failed (attempt %s/%s): %s", description, attempt, attempts, e)
            if attempt < attempts:
                await asyncio.sleep(base_delay * attempt)
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
        if i in assigned_set:
            blocks.append(f"\U0001F449 <b>{part['label']}:\n{joined}</b>")
        else:
            blocks.append(f"{part['label']}:\n{joined}")
    return f"<b>{song['title']}</b>\n\n" + BLOCK_SEPARATOR.join(blocks)


def build_prompt_text(song: dict, part_index: int, step: int, total: int) -> str:
    part = song["parts"][part_index]
    quoted = part_text(part, sep="\n")
    return (
        f"\U0001F3A5 Запишите часть {step}/{total}: <b>{part['label']}</b>\n"
        f"\u00ab{quoted}\u00bb\n\n"
        f"Пришлите видеокружок с этой частью. Можно перезаписывать сколько угодно "
        f"раз \u2014 засчитывается только последний вариант. Нажмите «Подтвердить», "
        f"когда будете готовы."
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

    db_add_registration(chat_id, user.id, display_name)
    registered_users.setdefault(chat_id, {})[user.id] = display_name

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
    global current_round

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
    song, filename, exhausted = load_random_song(exclude=played_songs.get(chat_id))
    if exhausted:
        # Every song has already been played for this group -- start a fresh
        # rotation rather than refusing to play.
        played_songs[chat_id] = set()
        logger.info("Song rotation for chat %s exhausted; resetting.", chat_id)
    played_songs.setdefault(chat_id, set()).add(filename)

    participant_ids = list(group_registrations.keys())
    assignments = assign_parts(song, participant_ids)

    r = Round(song, filename, update.effective_chat.id, participant_ids)
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
        lambda: update.message.reply_text(announcement, parse_mode=ParseMode.HTML),
        description="playsong: round start announcement",
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
        lambda: query.edit_message_text(f"\u2705 Зафиксировано: {r.song['parts'][part_index]['label']}"),
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
    """Immediately cancels the active round, wherever it's at, and notifies the
    group plus every participant who had a part assigned. Only an administrator
    or the creator of the round's group may do this. Can be invoked either from
    that group directly, or via DM (useful if the group chat itself is
    unreachable) -- either way, permission is checked against the round's
    group, not the chat the command was typed in.
    """
    global current_round
    r = current_round

    if r is None or not r.active:
        await call_with_retry(
            lambda: update.message.reply_text("Сейчас нет активного раунда для отмены."),
            description="cancel_command: no active round reply",
        )
        return

    chat = update.effective_chat
    user_id = update.effective_user.id
    called_from_group = chat.type in ("group", "supergroup") and chat.id == r.group_chat_id
    called_from_dm = chat.type == "private"

    if not (called_from_group or called_from_dm):
        await call_with_retry(
            lambda: update.message.reply_text(
                "Эту команду нужно вызвать в группе, где идёт раунд, либо в личных сообщениях."
            ),
            description="cancel_command: wrong context reply",
        )
        return

    if not await is_group_admin(context, r.group_chat_id, user_id):
        await call_with_retry(
            lambda: update.message.reply_text(
                "Отменять исполнение песни могут только администраторы или создатель группы."
            ),
            description="cancel_command: not admin reply",
        )
        return

    r.active = False
    if r.job is not None:
        r.job.schedule_removal()

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


async def finalize_round(context: ContextTypes.DEFAULT_TYPE):
    global current_round
    r = current_round
    if r is None or not r.active:
        return
    r.active = False

    if r.all_confirmed():
        intro = f"\U0001F3AC Песня готова: {r.song['title']}"
        if r.perk:
            intro += f"\n\U0001F3B2 Задание: {r.perk}"
        await call_with_retry(
            lambda: context.bot.send_message(chat_id=r.group_chat_id, text=intro),
            description="finalize_round: intro message",
        )
        for part_index in range(r.num_parts()):
            submission = r.submissions[part_index]
            label = r.song["parts"][part_index]["label"]
            performer = registered_users.get(r.group_chat_id, {}).get(
                submission["user_id"], "участник"
            )
            await call_with_retry(
                lambda label=label, performer=performer, part_index=part_index: context.bot.send_message(
                    chat_id=r.group_chat_id, text=f"Часть {part_index + 1}: {label} \u2014 {performer}"
                ),
                description=f"finalize_round: caption for part {part_index}",
            )
            # Keep going even if this one clip ultimately fails to send after
            # retries -- one dropped connection shouldn't cancel the rest of
            # the performance.
            await call_with_retry(
                lambda submission=submission: context.bot.send_video_note(
                    chat_id=r.group_chat_id, video_note=submission["file_id"]
                ),
                description=f"finalize_round: video note for part {part_index}",
            )
    else:
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    # Catches anything an individual handler didn't handle itself (e.g. a network
    # timeout on a call not wrapped in try/except). Logs it instead of letting it
    # silently vanish; the bot keeps running either way -- PTB isolates exceptions
    # per update.
    logger.error("Unhandled exception while processing update %s", update, exc_info=context.error)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Set the SONG_BOT_TOKEN environment variable.")

    init_db()
    global registered_users
    registered_users = load_registrations()
    total = sum(len(users) for users in registered_users.values())
    logger.info(
        "Loaded %s registration(s) across %s group(s) from %s",
        total, len(registered_users), DB_FILE,
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
        .build()
    )
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("register", register_command))
    app.add_handler(CommandHandler("unregister", unregister_command))
    app.add_handler(CommandHandler("playsong", playsong_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(MessageHandler(filters.VIDEO_NOTE, video_note_handler))
    app.add_handler(MessageHandler(filters.VIDEO, wrong_video_type_handler))
    app.add_handler(CallbackQueryHandler(confirm_callback, pattern="^confirm_take$"))
    app.add_error_handler(error_handler)

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()