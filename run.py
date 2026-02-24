#!/usr/bin/env python3
"""
Master script for orchestrating call processing.

Launches:
1. Krisp Downloader — download recordings from Krisp
2. Call Processor — transcription, summarization, action items

Usage:
    python run.py              # Full cycle (Krisp + processing)
    python run.py --no-krisp   # Processing only (without Krisp)
    python run.py --once       # Single pass and exit
"""

import argparse
import subprocess
import sys
import time
import signal
import logging
from pathlib import Path
from typing import List, Optional

from config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class ProcessManager:
    """Manage child processes."""

    def __init__(self):
        self.processes: List[subprocess.Popen] = []
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        logger.info("Stop signal received...")
        self.stop_all()
        sys.exit(0)

    def start(self, cmd: List[str], name: str, log_file: Optional[Path] = None) -> subprocess.Popen:
        """Start a process."""
        if log_file:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_handle = open(log_file, "a")
        else:
            log_handle = subprocess.DEVNULL

        proc = subprocess.Popen(
            cmd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            cwd=Path(__file__).parent,
        )
        self.processes.append(proc)
        logger.info("Started %s (PID: %d)", name, proc.pid)
        return proc

    def stop_all(self):
        """Stop all processes."""
        for proc in self.processes:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        logger.info("All processes stopped")

    def wait_all(self):
        """Wait for all processes to finish."""
        for proc in self.processes:
            proc.wait()


def run_krisp_downloader(config: Config, once: bool = False) -> List[str]:
    """Returns command to launch Krisp Downloader."""
    krisp_script = Path(__file__).parent / "krisp" / "downloader.py"

    cmd = [
        sys.executable, str(krisp_script),
        "--email", config.krisp_email,
        "--download-dir", config.watch_dir,
        "--check-interval", str(config.krisp_check_interval),
        "--headless",
    ]

    if once:
        cmd.append("--once")

    return cmd


def run_call_processor(once: bool = False) -> List[str]:
    """Returns command to launch Call Processor."""
    if once:
        return [sys.executable, str(Path(__file__).parent / "process.py"), "--once"]
    else:
        return [sys.executable, str(Path(__file__).parent / "process.py")]


def main():
    parser = argparse.ArgumentParser(description="Call processing master script")
    parser.add_argument("--no-krisp", action="store_true", help="Don't start Krisp Downloader")
    parser.add_argument("--once", action="store_true", help="Single pass and exit")
    args = parser.parse_args()

    config = Config()
    logs_dir = Path(__file__).parent / "logs"
    logs_dir.mkdir(exist_ok=True)

    logger.info("=" * 60)
    logger.info("Call Processing Master Script")
    logger.info("=" * 60)
    logger.info("Calls directory: %s", config.watch_dir)
    logger.info("Mode: %s", "single pass" if args.once else "monitoring")
    logger.info("=" * 60)

    if args.once:
        # Single pass — run sequentially
        logger.info("Starting processing (single pass)...")

        result = subprocess.run(
            run_call_processor(once=True),
            cwd=Path(__file__).parent,
        )

        if result.returncode == 0:
            logger.info("Processing completed successfully")
        else:
            logger.error("Processing error")
            sys.exit(1)
    else:
        # Monitoring — launch in parallel
        pm = ProcessManager()

        if not args.no_krisp:
            krisp_cmd = run_krisp_downloader(config)
            pm.start(krisp_cmd, "Krisp Downloader", logs_dir / "krisp_downloader.log")
            time.sleep(2)

        # Call Processor
        pm.start(run_call_processor(once=False), "Call Processor", logs_dir / "call_processor.log")

        logger.info("")
        logger.info("System started. Press Ctrl+C to stop.")
        logger.info("")

        try:
            pm.wait_all()
        except KeyboardInterrupt:
            pm.stop_all()


if __name__ == "__main__":
    main()
