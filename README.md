# Call Processing Pipeline

Automated meeting recording workflow: download → transcribe → summarize → action items

---

## Problem Statement

As a PM, post-meeting work is a tax on your time: scrubbing through recordings, writing up summaries, pulling out action items, and then manually entering them into a tracker. Multiply that by 4-6 calls per day and it becomes a serious bottleneck. This pipeline automates the entire workflow end-to-end — from the moment a recording lands in your downloads folder to having structured summaries and action items routed into YouTrack and a Markdown inbox.

---

## Architecture

```
┌─────────────┐    ┌──────────────┐    ┌─────────────┐    ┌──────────────┐    ┌──────────────┐
│  Krisp       │───▶│  CalDAV       │───▶│  Whisper     │───▶│  Claude CLI   │───▶│  YouTrack /  │
│  Download    │    │  Rename       │    │  Transcribe  │    │  Summarize    │    │  Markdown    │
└─────────────┘    └──────────────┘    └─────────────┘    └──────────────┘    └──────────────┘
   Playwright         Calendar            faster-whisper      Structured          Action items
   automation         event matching      + diarization       JSON output         routing
```

---

## Key Features

- **Local-first**: Whisper runs entirely on-device — no audio is sent to external services
- **Calendar-aware renaming**: Matches recordings to calendar events via CalDAV using ±15 min fuzzy time matching
- **Speaker diarization**: MFCC-based clustering identifies speakers without a GPU (tested on 8 GB RAM, M-series and Intel)
- **Structured AI summaries**: Claude CLI extracts meeting topics, decisions, and action items into a consistent JSON schema
- **Action item routing**: Classifies items against existing YouTrack tasks, creates subtasks, and posts meeting summaries as issue comments
- **UUID v7 trick**: Extracts exact recording timestamps from Krisp's UUIDv7 file IDs — no reliance on filesystem metadata
- **Dual-write inbox**: Action items are written to both YouTrack and a local Markdown kanban file for offline access

---

## Quick Start

```bash
git clone https://github.com/sbaleskov/call-processing.git
cd call-processing

# Install dependencies
pip install -r requirements.txt
playwright install chromium  # Only if using Krisp auto-download

# Configure
cp .env.example .env
# Edit .env with your credentials

# Run a single pass over the meetings directory
python process.py --once

# Run in monitoring mode (watches for new files)
python run.py
```

---

## Prerequisites

- Python 3.10+
- [Claude CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated (`claude` must be on PATH)
- `ffmpeg` for audio format conversion (optional, needed only for non-M4A inputs)
- ~2 GB disk space for the Whisper `medium` model (downloaded automatically on first run)

---

## Configuration

All configuration is loaded from a `.env` file in the project root. See `.env.example` for the full list. The most important variables:

| Variable | Description | Required | Default |
|---|---|---|---|
| `MEETINGS_DIR` | Directory to watch for new recordings | Yes | `./meetings` |
| `WHISPER_MODEL_SIZE` | Whisper model size (`tiny`, `base`, `small`, `medium`, `large`) | No | `medium` |
| `LANGUAGE` | Primary language for transcription (ISO 639-1 code) | No | `ru` |
| `CALDAV_URL` | CalDAV server URL for calendar event matching | Yes | — |
| `CALDAV_USERNAME` | CalDAV account username | Yes | — |
| `CALDAV_PASSWORD` | CalDAV account password | Yes | — |
| `CALDAV_CALENDAR_NAME` | Name of the calendar to query | Yes | — |
| `YOUTRACK_ENABLED` | Enable YouTrack integration | No | `false` |
| `YOUTRACK_URL` | YouTrack instance base URL | If enabled | — |
| `YOUTRACK_TOKEN` | YouTrack permanent API token | If enabled | — |
| `YOUTRACK_PROJECT` | YouTrack project short name | If enabled | — |
| `INBOX_ENABLED` | Enable Markdown inbox write | No | `false` |
| `INBOX_FILE` | Absolute path to the Markdown kanban file | If enabled | — |
| `KRISP_EMAIL` | Krisp account email for auto-download | If using downloader | — |
| `DIARIZE` | Enable speaker diarization | No | `true` |
| `NUM_SPEAKERS` | Expected number of speakers (0 = auto-detect) | No | `0` |

---

## Project Structure

```
call-processing/
├── run.py                   # Entry point: file watcher + monitoring loop
├── process.py               # Batch processor: run once or on a schedule
├── config.py                # Central config, loaded from .env
│
├── pipeline/
│   ├── handler.py           # Core pipeline logic (download → rename → transcribe → route)
│   ├── transcribe.py        # faster-whisper wrapper with diarization
│   ├── summarize.py         # Claude CLI invocation + JSON parsing
│   └── calendar.py          # CalDAV calendar event matching
│
├── integrations/
│   ├── youtrack.py          # YouTrack REST API client
│   └── inbox.py             # Markdown inbox writer
│
├── krisp/
│   ├── downloader.py        # Playwright-based Krisp web UI automation
│   ├── bulk_download.py     # Batch download for backfill
│   ├── auth.py              # Session/cookie management
│   └── auth_setup.py        # One-time auth setup helper
│
├── utils/
│   ├── krisp_id.py          # UUID v7 timestamp extraction
│   └── sanitize.py          # Filename sanitization helpers
│
└── scripts/
    ├── rename_downloaded.py  # Rename downloaded recordings using calendar + UUID
    ├── rename_existing.py    # Rename old-format recordings
    └── cleanup.py            # Remove duplicate recordings
```

Output files are written alongside each audio file:

```
meetings/
├── 250114_Client Meeting_019bbc2e.mp3
├── 250114_Client Meeting_019bbc2e_transcription.txt
└── 250114_Client Meeting_019bbc2e_summary.md
```

---

## How It Works

1. **Download** — Playwright automates the Krisp web UI to download recordings. Alternatively, point `MEETINGS_DIR` at your Downloads folder and let the watcher pick up files manually.

2. **Rename** — Extracts the exact recording timestamp from Krisp's UUIDv7 file ID (first 12 hex chars encode Unix milliseconds), then queries CalDAV to find a matching calendar event within ±15 minutes. The file is renamed to `YYMMDD_EventTitle_krispID.ext`.

3. **Transcribe** — `faster-whisper` runs locally in `int8` mode with the configured model size. If diarization is enabled, MFCC features are extracted and clustered (k-means) to assign speaker labels before final transcription.

4. **Summarize** — The transcription is passed to Claude CLI via a structured prompt. Output is parsed as JSON containing: project, participants, topics discussed, decisions made, and action items with owners and deadlines.

5. **Route** — Action items are matched against open YouTrack tasks using keyword similarity. Matched items are added as subtasks; unmatched ones are created as new tasks. The meeting summary is posted as a comment on the relevant issue. All items are also appended to the local Markdown inbox.

---

## Known Limitations

- Krisp web UI automation is fragile — any UI update from Krisp can break the Playwright selectors and require manual patching
- Speaker diarization accuracy degrades with poor recording quality, loud background noise, or speakers with similar vocal characteristics
- Calendar matching uses a ±15 min window — back-to-back meetings or rescheduled calls may cause mismatches
- Claude CLI must be pre-authenticated; the pipeline does not manage API keys or handle authentication failures gracefully
- YouTrack task classification is keyword-based and works best when task titles are descriptive

---

## License

MIT
