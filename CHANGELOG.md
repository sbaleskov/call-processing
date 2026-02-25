# Changelog

## 2026-02-25 — Remote transcription + configurable schedule

### Remote transcription mode (`TRANSCRIBE_REMOTE`)

Offload whisper + diarization to a remote Linux server via SSH. Audio is uploaded
temporarily, transcribed on the server, and the result pulled back. All output
files stay local.

**New files:**
- `remote/worker.py` — self-contained transcription CLI for the server
- `remote/deploy.sh` — one-command deployment: `./remote/deploy.sh user@host`
- `pipeline/transcribe_remote.py` — SSH orchestration (scp + ssh)

**Changed files:**
- `pipeline/transcribe.py` — `transcribe_audio()` now dispatches to local or remote
- `config.py` — added `TRANSCRIBE_REMOTE`, `REMOTE_HOST`, `REMOTE_USER`, `REMOTE_SSH_KEY`, `REMOTE_WORKER_DIR`, `REMOTE_WORK_DIR`

**New `.env` variables:**
```
TRANSCRIBE_REMOTE=false          # false = local | true = remote server via SSH
REMOTE_HOST=                     # server IP or hostname
REMOTE_USER=root                 # SSH user
REMOTE_SSH_KEY=                  # path to key (empty = default)
REMOTE_WORKER_DIR=/opt/call-processing
REMOTE_WORK_DIR=/tmp/call-processing
```

### Configurable processing schedule (`SCHEDULE_MODE`)

Replaced the hardcoded 1-hour loop with two configurable modes:
- `window` — check every N minutes within a time window (e.g. every 2h during 12:00-23:30)
- `daily` — single run at a fixed time each day

**Changed files:**
- `process.py` — rewritten scheduler with `loop_window()` and `loop_daily()`
- `config.py` — added `SCHEDULE_MODE`, `SCHEDULE_WINDOW`, `SCHEDULE_INTERVAL`, `SCHEDULE_TIME`

**New `.env` variables:**
```
SCHEDULE_MODE=window             # window | daily
SCHEDULE_WINDOW=12:00-23:30      # active hours (window mode)
SCHEDULE_INTERVAL=120            # minutes between checks (window mode)
SCHEDULE_TIME=23:00              # run time (daily mode)
```

### Infra

- Removed old nightly whisper cron (`/opt/automation/whisper/transcribe.py`) from VPS
- Installed `ffmpeg` on VPS (was missing, needed for non-WAV formats)
- Deployed `worker.py` to VPS at `/opt/call-processing/` with dedicated venv

### Updated docs

- `README.md` — added "Remote transcription" section, added schedule vars to config table
- `.env.example` — documented all new variables
