# Call Processing Pipeline

A PM tool. Takes meeting recordings, produces transcriptions, summaries, and files action items into YouTrack. Built for myself — I do 4-6 calls a day and got tired of the post-call paperwork.

## How it works

```
python run.py
  │
  ├── krisp/downloader.py          Grabs recordings from Krisp via Playwright
  │     └── pipeline/calendar.py   Renames file by calendar event
  │
  └── process.py                   For each new audio file:
        └── pipeline/handler.py
              ├── pipeline/calendar.py      ① Rename → match to calendar via CalDAV
              ├── pipeline/transcribe.py    ② Transcribe → faster-whisper + diarization
              ├── pipeline/summarize.py     ③ Summarize → Claude CLI → JSON
              ├── integrations/youtrack.py  ④ Subtasks + summary comment in YT
              └── integrations/inbox.py     ④ Same items → local Markdown kanban
```

You launch `run.py`, it keeps the downloader and processor alive. Without Krisp — `python process.py --once`.

`scripts/` — one-off utilities (batch rename, cleanup). Not part of the main loop.

## Output

```
meetings/
├── 250114_Client Meeting_019bbc2e.mp3
├── 250114_Client Meeting_019bbc2e_transcription.txt
└── 250114_Client Meeting_019bbc2e_summary.md
```

## A few things worth noting

Transcription is local (faster-whisper, int8). No audio goes anywhere. Runs on an M1 Air 8 GB with the `medium` model.

Krisp saves recordings as hex UUIDs. Turns out these are UUIDv7 — first 48 bits encode a Unix timestamp in milliseconds. The pipeline extracts the time, queries CalDAV for a matching event (±15 min window), and renames the file to `YYMMDD_EventTitle_krispID.ext`.

Diarization is MFCC + k-means, no GPU needed. Works okay for 2-4 people if the audio quality is reasonable.

Action items go two places: YouTrack (as subtasks of existing issues, with the summary posted as a comment) and a local Markdown file (for offline).

## Setup

```bash
git clone https://github.com/sbaleskov/call-processing.git
cd call-processing
pip install -r requirements.txt
playwright install chromium  # only for Krisp auto-download

cp .env.example .env         # fill in credentials
python process.py --once     # or: python run.py
```

Needs Python 3.10+, [Claude CLI](https://docs.anthropic.com/en/docs/claude-code) on PATH. Whisper model (~2 GB) downloads on first run. `ffmpeg` optional.

## Configuration

All in `.env`, see `.env.example`. Short version:

| Variable | What | Required |
|---|---|---|
| `MEETINGS_DIR` | Audio files directory | Yes |
| `CALDAV_URL`, `_USERNAME`, `_PASSWORD`, `_CALENDAR_NAME` | Calendar for renaming | Yes |
| `WHISPER_MODEL_SIZE` | `tiny`/`base`/`small`/`medium`/`large` | No (`medium`) |
| `LANGUAGE` | ISO 639-1 | No (`ru`) |
| `YOUTRACK_ENABLED`, `_URL`, `_TOKEN`, `_PROJECT` | Task tracker | No |
| `INBOX_ENABLED`, `_FILE` | Markdown kanban path | No |
| `KRISP_EMAIL` | Auto-downloader | No |
| `DIARIZE` / `NUM_SPEAKERS` | Speaker separation | No (`true` / `0`) |

## Known issues

- Krisp downloader automates their web UI with Playwright. Any UI update on their side breaks it.
- Diarization degrades with noise, bad mics, or similar voices.
- Calendar matching has a ±15 min window — back-to-back calls can mismatch.
- Claude CLI needs to be authenticated beforehand; no auth error handling here.
- YouTrack task matching is keyword-based — vague task titles = wrong matches.

## License

MIT
