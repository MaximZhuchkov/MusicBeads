"""
Bare-bones song relay bot (Russian UI, multi-line song parts).

Flow:
  1. Participants DM the bot /start to register (kept in memory, no group check yet).
  2. Someone types /playsong in the group -> bot picks a random song, round-robin
     assigns parts to registered participants, and DMs each their part(s).
  3. If a participant has multiple parts, they're prompted one at a time. They can
     send a video circle repeatedly (each overwrites the last) and tap "Confirm"
     when happy, which locks in that part and advances to the next one.
  4. After ROUND_DURATION_SECONDS, the bot checks whether every part was confirmed.
     If yes, it posts each video circle to the group in song order. If no, it
     announces the song as incomplete and the round ends.

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
  - No check that a /start'd user is actually a member of the target group.
  - Only one round can be active at a time, globally.
  - State is in-memory only -- a restart loses any in-progress round.
"""

import json
import logging
import os
import random
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("song_bot")

BOT_TOKEN = os.environ.get("SONG_BOT_TOKEN")
SONGS_DIR = Path(__file__).parent / "songs"
ROUND_DURATION_SECONDS = int(os.environ.get("ROUND_DURATION_SECONDS", 300))

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

registered_users: dict[int, str] = {}  # user_id -> display name


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

    def num_parts(self) -> int:
        return len(self.song["parts"])

    def all_confirmed(self) -> bool:
        return len(self.submissions) == self.num_parts()


current_round: Optional[Round] = None

# ---------------------------------------------------------------------------
# Song loading
# ---------------------------------------------------------------------------


def load_random_song() -> tuple[dict, str]:
    files = sorted(SONGS_DIR.glob("*.json"))
    if not files:
        raise RuntimeError(f"No song files found in {SONGS_DIR}")
    path = random.choice(files)
    with open(path, "r", encoding="utf-8") as f:
        song = json.load(f)
    return song, path.name


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


def build_overview_message(song: dict, assigned_indices: list[int]) -> str:
    assigned_set = set(assigned_indices)
    lines = [f"<b>{song['title']}</b>", ""]
    for i, part in enumerate(song["parts"]):
        joined = part_text(part, sep=" ")
        if i in assigned_set:
            lines.append(f"\U0001F449 <b>{part['label']}: {joined}</b>")
        else:
            lines.append(f"{part['label']}: {joined}")
    return "\n".join(lines)


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
    user = update.effective_user
    registered_users[user.id] = user.first_name or user.username or str(user.id)
    await update.message.reply_text(
        "Вы зарегистрированы! Когда в группе начнётся раунд, я пришлю вам сюда вашу партию."
    )
    logger.info("Registered user %s (%s)", user.id, registered_users[user.id])


async def send_next_prompt(context: ContextTypes.DEFAULT_TYPE, participant_id: int):
    r = current_round
    pointer = r.current_pointer[participant_id]
    total = len(r.assignments[participant_id])
    if pointer >= total:
        return
    part_index = r.assignments[participant_id][pointer]
    text = build_prompt_text(r.song, part_index, pointer + 1, total)
    await context.bot.send_message(chat_id=participant_id, text=text, parse_mode=ParseMode.HTML)


async def playsong_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_round

    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Запустите /playsong в групповом чате.")
        return

    if current_round is not None and current_round.active:
        await update.message.reply_text("Раунд уже идёт.")
        return

    if not registered_users:
        await update.message.reply_text(
            "Пока никто не зарегистрировался. Попросите участников написать мне /start в личные сообщения."
        )
        return

    song, filename = load_random_song()
    participant_ids = list(registered_users.keys())
    assignments = assign_parts(song, participant_ids)

    r = Round(song, filename, update.effective_chat.id, participant_ids)
    r.assignments = assignments
    r.current_pointer = {pid: 0 for pid in participant_ids}
    current_round = r

    await update.message.reply_text(
        f"\U0001F3B5 Начинаем раунд: <b>{song['title']}</b>\n"
        f"Проверьте личные сообщения \u2014 там ваша партия. "
        f"У вас есть {ROUND_DURATION_SECONDS // 60} минут.",
        parse_mode=ParseMode.HTML,
    )

    for pid in participant_ids:
        if not assignments[pid]:
            continue  # more participants than parts; this one sits out this round
        try:
            await context.bot.send_message(
                chat_id=pid,
                text=build_overview_message(song, assignments[pid]),
                parse_mode=ParseMode.HTML,
            )
            await send_next_prompt(context, pid)
        except Exception as e:
            logger.warning("Could not DM participant %s: %s", pid, e)

    context.job_queue.run_once(finalize_round, ROUND_DURATION_SECONDS, name="finalize_round")


async def video_note_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    r = current_round
    user_id = update.effective_user.id

    if r is None or not r.active or user_id not in r.assignments:
        return  # no active round expects anything from this user

    pointer = r.current_pointer.get(user_id, 0)
    total = len(r.assignments[user_id])
    if pointer >= total:
        await update.message.reply_text("Вы уже отправили все свои части в этом раунде.")
        return

    file_id = update.message.video_note.file_id
    r.pending_take[user_id] = file_id
    await update.message.reply_text(
        "Принято! Пришлите другой дубль, чтобы заменить, или нажмите «Подтвердить», когда будете готовы.",
        reply_markup=CONFIRM_BUTTON,
    )


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    r = current_round
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    if r is None or not r.active or user_id not in r.assignments:
        await query.edit_message_text("Этот раунд уже завершён.")
        return

    take = r.pending_take.get(user_id)
    if take is None:
        await query.edit_message_text("Сначала пришлите видеокружок, потом подтвердите.")
        return

    pointer = r.current_pointer[user_id]
    part_index = r.assignments[user_id][pointer]
    r.submissions[part_index] = {"file_id": take, "user_id": user_id}
    del r.pending_take[user_id]
    r.current_pointer[user_id] += 1

    await query.edit_message_text(f"\u2705 Зафиксировано: {r.song['parts'][part_index]['label']}")

    if r.current_pointer[user_id] < len(r.assignments[user_id]):
        await send_next_prompt(context, user_id)
    else:
        await context.bot.send_message(
            chat_id=user_id, text="Вы всё сделали! Ждём остальных участников группы."
        )


async def finalize_round(context: ContextTypes.DEFAULT_TYPE):
    global current_round
    r = current_round
    if r is None or not r.active:
        return
    r.active = False

    if r.all_confirmed():
        await context.bot.send_message(
            chat_id=r.group_chat_id, text=f"\U0001F3AC Песня готова: {r.song['title']}"
        )
        for part_index in range(r.num_parts()):
            submission = r.submissions[part_index]
            label = r.song["parts"][part_index]["label"]
            performer = registered_users.get(submission["user_id"], "участник")
            await context.bot.send_message(
                chat_id=r.group_chat_id, text=f"Часть {part_index + 1}: {label} \u2014 {performer}"
            )
            await context.bot.send_video_note(chat_id=r.group_chat_id, video_note=submission["file_id"])
    else:
        missing = r.num_parts() - len(r.submissions)
        await context.bot.send_message(
            chat_id=r.group_chat_id,
            text=(
                f"\u274c Песня {r.filename} не была исполнена полностью "
                f"(не хватает частей: {missing}). Введите /playsong ещё раз, чтобы выбрать новую песню."
            ),
        )

    current_round = None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Set the SONG_BOT_TOKEN environment variable.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("playsong", playsong_command))
    app.add_handler(MessageHandler(filters.VIDEO_NOTE, video_note_handler))
    app.add_handler(CallbackQueryHandler(confirm_callback, pattern="^confirm_take$"))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
