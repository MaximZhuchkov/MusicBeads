"""Convert plain-text songs in unparsed_songs/ into the JSON format songs/
expects, then move them there.

Input format (one file per song, filename becomes the JSON filename):
  line 1:  "Performer - Title" (used verbatim as the JSON "title")
  rest:    lyrics, split into parts by one or more blank lines and/or lines
           that consist only of emoji (a divider some sources use instead of,
           or alongside, a blank line). A divider line itself is dropped, not
           kept as lyric content.

Run with: python parse_songs.py
"""

from pathlib import Path
import json
import re

UNPARSED_DIR = Path(__file__).parent / "unparsed_songs"
SONGS_DIR = Path(__file__).parent / "songs"

# Ranges covering the common emoji blocks (pictographs, emoticons, dingbats,
# flags, misc symbols) plus the modifiers that glue them into one glyph
# (variation selector, zero-width joiner, skin tones).
_EMOJI_CHARS = (
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002B00-\U00002BFF"
    "\U0000FE0F"
    "\U0000200D"
)
_EMOJI_LINE_RE = re.compile(f"^[{_EMOJI_CHARS}\\s]+$")


def is_section_break(line: str) -> bool:
    stripped = line.strip()
    return not stripped or bool(_EMOJI_LINE_RE.match(stripped))


def parse_song(text: str) -> dict:
    lines = text.splitlines()
    title = lines[0].strip() if lines else ""
    if not title:
        raise ValueError("missing title (first line is empty)")

    parts = []
    current = []
    for line in lines[1:]:
        if is_section_break(line):
            if current:
                parts.append(current)
                current = []
        else:
            current.append(line.rstrip())
    if current:
        parts.append(current)
    if not parts:
        raise ValueError("no lyrics found after the title")

    return {"title": title, "parts": [{"lines": p} for p in parts]}


def parse_unparsed_songs() -> list[str]:
    """Parses every file in unparsed_songs/ into songs/, deleting each source
    file once it's been converted. Returns the titles of the songs that were
    successfully parsed this call -- since a source file is removed as soon as
    it's parsed, a given song is only ever reported once across calls (e.g. by
    a caller that polls this periodically).
    """
    SONGS_DIR.mkdir(exist_ok=True)
    if not UNPARSED_DIR.exists():
        return []

    titles = []
    for src in sorted(UNPARSED_DIR.iterdir()):
        if not src.is_file():
            continue
        try:
            song = parse_song(src.read_text(encoding="utf-8"))
        except ValueError as e:
            print(f"Skipping {src.name}: {e}")
            continue

        dest = SONGS_DIR / f"{src.stem}.json"
        with dest.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(song, f, ensure_ascii=False, indent=2)
            f.write("\n")
        src.unlink()
        print(f"{src.name} -> {dest.relative_to(Path(__file__).parent)}")
        titles.append(song["title"])

    return titles


def main():
    parse_unparsed_songs()


if __name__ == "__main__":
    main()
