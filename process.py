#!/usr/bin/env python3
"""
Process new meeting audio files.

Modes:
- --once: single pass (process all new files and exit)
- No flags: scheduled loop controlled by SCHEDULE_MODE in .env

Schedule modes (see .env):
- window: check every SCHEDULE_INTERVAL minutes during SCHEDULE_WINDOW (e.g. 12:00-23:30)
- daily:  single run at SCHEDULE_TIME each day (e.g. 23:00)
"""

import argparse
import os
import time
import sys
from pathlib import Path
from datetime import datetime, timedelta
import logging

# Lower process priority to avoid system load
try:
    os.nice(10)
except OSError:
    pass

# Limit OpenMP/MKL threads (in case CTranslate2 uses them directly)
os.environ.setdefault("OMP_NUM_THREADS", "4")
os.environ.setdefault("MKL_NUM_THREADS", "4")

try:
    import setproctitle
    setproctitle.setproctitle("Audio Transcript Processor")
except ImportError:
    if sys.platform == "darwin":
        try:
            sys.argv[0] = "Audio Transcript Processor"
        except Exception:
            pass

from pipeline.handler import AudioFileHandler
from config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent / "process_new_files.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


def run_once():
    """Single pass: find unprocessed files and process them."""
    config = Config()
    watch_dir = Path(config.watch_dir)

    if not watch_dir.exists():
        logger.warning("Directory %s does not exist", watch_dir)
        return

    handler = AudioFileHandler(config)
    unprocessed = handler.get_unprocessed_files(watch_dir)

    if not unprocessed:
        logger.info("No new files to process")
        return

    logger.info("Found %d new file(s) to process", len(unprocessed))

    for file_path in unprocessed:
        logger.info("Processing file: %s", file_path.name)
        try:
            handler.process_file(file_path)
        except Exception as e:
            logger.error("Error processing %s: %s", file_path.name, e, exc_info=True)

    logger.info("Processing complete")


# ---------------------------------------------------------------------------
# Schedule helpers
# ---------------------------------------------------------------------------

def _parse_time(s: str) -> tuple:
    """Parse 'HH:MM' → (hour, minute). Validates range."""
    h, m = s.strip().split(":")
    h, m = int(h), int(m)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Invalid time: {s!r} (expected HH:MM, 00:00-23:59)")
    return h, m


def _in_window(now: datetime, window: str) -> bool:
    """Check if `now` is within a 'HH:MM-HH:MM' window."""
    start_s, end_s = window.split("-")
    sh, sm = _parse_time(start_s)
    eh, em = _parse_time(end_s)

    start_min = sh * 60 + sm
    end_min = eh * 60 + em
    now_min = now.hour * 60 + now.minute

    if start_min <= end_min:
        return start_min <= now_min <= end_min
    else:
        # Overnight window (e.g. 22:00-06:00)
        return now_min >= start_min or now_min <= end_min


def _seconds_until(target_h: int, target_m: int) -> float:
    """Seconds from now until the next occurrence of HH:MM today or tomorrow."""
    now = datetime.now()
    target = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


# ---------------------------------------------------------------------------
# Loop modes
# ---------------------------------------------------------------------------

def loop_window(config: Config):
    """Check for new files every SCHEDULE_INTERVAL minutes within SCHEDULE_WINDOW."""
    interval = config.schedule_interval * 60  # to seconds
    window = config.schedule_window

    logger.info("Schedule: window mode — every %d min during %s", config.schedule_interval, window)

    while True:
        try:
            now = datetime.now()

            if _in_window(now, window):
                logger.info("Checking for new files — %s", now.strftime("%H:%M:%S"))
                run_once()

                # Sleep interval or until window ends, whichever comes first
                eh, em = _parse_time(window.split("-")[1])
                end_secs = _seconds_until(eh, em)
                sleep_secs = min(interval, end_secs)
                logger.info("Next check in %.0f min", sleep_secs / 60)
                time.sleep(sleep_secs)
            else:
                # Outside window — sleep 1 min and recheck
                sh, sm = _parse_time(window.split("-")[0])
                wait = _seconds_until(sh, sm)
                logger.info(
                    "Outside schedule window (%s). Sleeping until %s (%.0f min)",
                    window, window.split("-")[0], wait / 60,
                )
                time.sleep(min(wait, 60))

        except KeyboardInterrupt:
            logger.info("Stop signal received. Shutting down...")
            break
        except Exception as e:
            logger.error("Critical loop error: %s", e, exc_info=True)
            time.sleep(60)


def loop_daily(config: Config):
    """Run once per day at SCHEDULE_TIME."""
    th, tm = _parse_time(config.schedule_time)
    logger.info("Schedule: daily mode — run at %s", config.schedule_time)

    while True:
        try:
            wait = _seconds_until(th, tm)
            logger.info("Next run at %s (in %.0f min)", config.schedule_time, wait / 60)
            time.sleep(wait)

            logger.info("Daily run starting — %s", datetime.now().strftime("%H:%M:%S"))
            run_once()

            # Sleep 61 seconds to avoid re-triggering in the same minute
            time.sleep(61)

        except KeyboardInterrupt:
            logger.info("Stop signal received. Shutting down...")
            break
        except Exception as e:
            logger.error("Critical loop error: %s", e, exc_info=True)
            time.sleep(60)


def main():
    parser = argparse.ArgumentParser(description="Process new meeting audio files")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Single pass and exit (default: scheduled loop)",
    )
    args = parser.parse_args()

    if args.once:
        logger.info("Starting in single-pass mode (--once)")
        run_once()
        return

    config = Config()

    if config.schedule_mode == "daily":
        loop_daily(config)
    else:
        loop_window(config)


if __name__ == "__main__":
    main()
