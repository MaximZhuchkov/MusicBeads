"""Combines a song round's per-part video-note clips into a single video, via
ffmpeg. This module is a pure processing utility -- no Telegram command
handling, permission checks, or settings persistence live here; see bot.py
for the /concat command, the group_settings table that stores whether a
group has this turned on, and the call site in finalize_round.

Requires the `ffmpeg` binary on PATH. If it's missing, or any step of the
download/encode pipeline fails, build_concatenated_video raises
VideoConcatError with a message detailed enough to diagnose from the log.
Per product decision, a failure here must never produce a group-facing
message and must never affect the individual video-note clips, which are
sent by bot.py independently of (and before) this module runs -- callers
are expected to catch VideoConcatError, log it, and simply skip posting the
combined video.
"""

import asyncio
import logging
import shutil
from pathlib import Path

from telegram import Bot

logger = logging.getLogger("song_bot.video_concat")

# Common square resolution every clip is normalized to before concatenation.
# Telegram video notes vary in diameter (roughly 240-640px) depending on the
# sending device, and ffmpeg's concat filter requires matching dimensions
# (and sample format) across all inputs.
TARGET_SIZE = 480
AUDIO_SAMPLE_RATE = 44100

FFMPEG_TIMEOUT_SECONDS = 120


class VideoConcatError(Exception):
    """Raised when building the concatenated video fails at any step:
    ffmpeg missing, a clip download error, or a non-zero ffmpeg exit."""


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


async def _download_clip(bot: Bot, file_id: str, dest: Path) -> None:
    try:
        tg_file = await bot.get_file(file_id)
        await tg_file.download_to_drive(custom_path=str(dest))
    except Exception as e:
        raise VideoConcatError(f"failed to download clip (file_id={file_id}): {e}") from e


async def _concat_clips(clip_paths: list[Path], output_path: Path) -> None:
    """Re-encodes and concatenates `clip_paths` (in order) into a single file
    at `output_path`, via one ffmpeg invocation using filter_complex concat
    (rather than the concat demuxer, since normalizing resolution/audio
    format already requires re-encoding every input anyway).
    """
    filter_parts = []
    stream_labels = []
    for i in range(len(clip_paths)):
        filter_parts.append(
            f"[{i}:v]scale={TARGET_SIZE}:{TARGET_SIZE}:force_original_aspect_ratio=decrease,"
            f"pad={TARGET_SIZE}:{TARGET_SIZE}:(ow-iw)/2:(oh-ih)/2,setsar=1[v{i}]"
        )
        filter_parts.append(
            f"[{i}:a]aformat=sample_rates={AUDIO_SAMPLE_RATE}:channel_layouts=stereo[a{i}]"
        )
        stream_labels.append(f"[v{i}][a{i}]")
    filter_parts.append(f"{''.join(stream_labels)}concat=n={len(clip_paths)}:v=1:a=1[outv][outa]")
    filter_complex = ";".join(filter_parts)

    args = ["ffmpeg", "-y"]
    for path in clip_paths:
        args += ["-i", str(path)]
    args += [
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-movflags", "+faststart",
        str(output_path),
    ]

    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=FFMPEG_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise VideoConcatError(f"ffmpeg timed out after {FFMPEG_TIMEOUT_SECONDS}s")

    if proc.returncode != 0:
        raise VideoConcatError(
            f"ffmpeg exited with code {proc.returncode}: "
            f"{stderr.decode(errors='replace')[-2000:]}"
        )
    logger.info("ffmpeg concatenated %s clip(s) into %s", len(clip_paths), output_path)


async def build_concatenated_video(bot: Bot, file_ids: list[str], work_dir: Path) -> Path:
    """Downloads each clip in `file_ids` (song-part order) into `work_dir`
    and concatenates them into a single normalized MP4 at
    `work_dir/combined.mp4`, which is returned.

    Raises VideoConcatError on any failure. `work_dir` is created if it
    doesn't exist; the caller owns its lifecycle (e.g. a
    tempfile.TemporaryDirectory) and is responsible for cleaning it up once
    the returned file has been used.
    """
    if not file_ids:
        raise VideoConcatError("no clips to concatenate")

    if not ffmpeg_available():
        raise VideoConcatError("ffmpeg binary not found on PATH")

    work_dir.mkdir(parents=True, exist_ok=True)

    clip_paths = []
    for i, file_id in enumerate(file_ids):
        dest = work_dir / f"part_{i:03d}.mp4"
        await _download_clip(bot, file_id, dest)
        clip_paths.append(dest)

    output_path = work_dir / "combined.mp4"
    await _concat_clips(clip_paths, output_path)
    return output_path
