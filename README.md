# Song relay bot (v1, no combined video)

## Setup

```bash
pip install -r requirements.txt
export SONG_BOT_TOKEN="your-bot-token"
python bot__.py
```

Requires Python 3.9+.

## Try it out

1. Have each participant DM the bot `/start`.
2. Add the bot to your test group (needs permission to send messages there).
3. Type `/playsong` in the group.
4. Each participant checks their DMs, sends a video circle for their assigned
   part(s), and taps **Confirm** when happy with the take.
5. After 5 minutes (`ROUND_DURATION_SECONDS` env var to change it), the bot
   either posts every clip to the group in order, or announces the song as
   incomplete if anything is missing.

## Known v1 simplifications

- Anyone who has ever DM'd `/start` is treated as eligible for every round --
  there's no check that they're actually in the group that ran `/playsong`.
- Only one round can run at a time, globally (not per-group).
- All state is in memory -- restarting the process loses any in-progress round.
- If participants outnumber song parts, the extras are simply not assigned
  anything that round.
