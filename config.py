"""Central configuration — loads settings from .env file.

All personal data (credentials, paths, tokens) comes from environment
variables. Defaults are generic and safe for public repositories.
"""

import os
from pathlib import Path
from typing import List, Optional
from dotenv import load_dotenv

# Project root = directory containing this file
_PROJECT_DIR = Path(__file__).parent.resolve()

# Load .env from project root
_env_path = _PROJECT_DIR / ".env"
if _env_path.exists():
    load_dotenv(_env_path)


class Config:
    """Application configuration loaded from environment variables."""

    def __init__(self, env_file: Optional[str] = None):
        if env_file:
            load_dotenv(env_file, override=True)

        # Directories
        self.meetings_dir: str = os.getenv(
            "MEETINGS_DIR", str(_PROJECT_DIR / "meetings")
        )
        self.watch_dir: str = os.getenv("WATCH_DIR", self.meetings_dir)
        self.output_dir: str = os.getenv("OUTPUT_DIR", self.meetings_dir)

        # Whisper transcription
        self.whisper_model_size: str = os.getenv("WHISPER_MODEL_SIZE", "medium")
        self.whisper_device: str = os.getenv("WHISPER_DEVICE", "cpu")
        self.whisper_compute_type: str = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
        self.language: str = os.getenv("LANGUAGE", "ru")
        self.cpu_threads: int = int(os.getenv("WHISPER_CPU_THREADS", "4"))

        # Audio file handling
        self.audio_extensions: List[str] = [
            ext.strip()
            for ext in os.getenv("AUDIO_EXTENSIONS", ".m4a,.mp3,.wav,.aac,.mov").split(",")
        ]
        self.file_stability_wait: int = int(os.getenv("FILE_STABILITY_WAIT", "5"))

        # Speaker diarization
        self.diarize: bool = os.getenv("DIARIZE", "true").lower() == "true"
        self.num_speakers: int = int(os.getenv("NUM_SPEAKERS", "0"))

        # CalDAV calendar
        self.caldav_url: str = os.getenv("CALDAV_URL", "")
        self.caldav_username: str = os.getenv("CALDAV_USERNAME", "")
        self.caldav_password: str = os.getenv("CALDAV_PASSWORD", "")
        self.caldav_calendar_name: str = os.getenv("CALDAV_CALENDAR_NAME", "")

        # YouTrack integration (optional)
        self.youtrack_enabled: bool = os.getenv("YOUTRACK_ENABLED", "false").lower() == "true"
        self.youtrack_url: str = os.getenv("YOUTRACK_URL", "")
        self.youtrack_token: str = os.getenv("YOUTRACK_TOKEN", "")
        self.youtrack_project: str = os.getenv("YOUTRACK_PROJECT", "")

        # Markdown inbox integration (optional)
        self.inbox_file: str = os.getenv("INBOX_FILE", "")
        self.inbox_enabled: bool = os.getenv("INBOX_ENABLED", "false").lower() == "true"

        # Krisp settings
        self.krisp_email: str = os.getenv("KRISP_EMAIL", "")
        self.krisp_check_interval: int = int(os.getenv("KRISP_CHECK_INTERVAL", "300"))
        self.krisp_downloads_dir: str = os.getenv("KRISP_DOWNLOADS_DIR", "~/Downloads")
