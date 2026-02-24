#!/usr/bin/env python3
"""
Process new meeting audio files.

Modes:
- --once: single pass (process all new files and exit)
- No flags: loop once per hour
"""

import argparse
import os
import time
import sys
from pathlib import Path
from datetime import datetime
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
        logging.FileHandler("process_new_files.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

CHECK_INTERVAL = 60 * 60  # 1 hour


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


def main():
    parser = argparse.ArgumentParser(description="Process new meeting audio files")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Single pass and exit (default: hourly loop)",
    )
    args = parser.parse_args()

    if args.once:
        logger.info("Starting in single-pass mode (--once)")
        run_once()
        return

    logger.info("Starting automatic processing, interval: 1 hour")
    while True:
        try:
            logger.info("Checking for new files — %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            run_once()
            logger.info("Waiting 1 hour until next check...")
            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            logger.info("Stop signal received. Shutting down...")
            break
        except Exception as e:
            logger.error("Critical loop error: %s", e, exc_info=True)
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
