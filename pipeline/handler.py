#!/usr/bin/env python3
"""
Call processing automation:
1. Detect unprocessed audio files
2. Rename: UUID timestamp → CalDAV calendar → YYMMDD_EventName_krispID
3. Transcribe via local faster-whisper
4. Summarize and extract action items via Claude CLI
5. Add action items to task inbox
6. Create subtasks in YouTrack + post summary comment
"""

import re
import time
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from pipeline.transcribe import transcribe_audio
from pipeline.summarize import summarize_transcription
from config import Config
from integrations.inbox import InboxManager
from integrations.youtrack import YouTrackClient
from pipeline.calendar import find_event_name
from utils.krisp_id import krisp_id_to_datetime
from utils.sanitize import sanitize_title

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent.parent / "call_processor.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


class AudioFileHandler(FileSystemEventHandler):
    """Audio file handler."""

    def __init__(self, config: Config):
        self.config = config
        self.processed_files = set()
        self.processing_lock = {}

    # ──────────────────────────────── watchdog events ─────────────────────────

    def on_created(self, event):
        if event.is_directory:
            return
        file_path = Path(event.src_path)
        if not self._is_audio_file(file_path):
            return
        logger.info("New audio file detected: %s", file_path.name)
        time.sleep(2)
        if self._is_file_stable(file_path):
            self.process_file(file_path)

    def on_modified(self, event):
        if event.is_directory:
            return
        file_path = Path(event.src_path)
        if not self._is_audio_file(file_path):
            return
        if str(file_path) in self.processing_lock or str(file_path) in self.processed_files:
            return
        if self._is_file_stable(file_path):
            self.process_file(file_path)

    # ──────────────────────────────── helpers ─────────────────────────────────

    def _is_audio_file(self, file_path: Path) -> bool:
        if file_path.suffix.lower() not in self.config.audio_extensions:
            return False
        if file_path.name.startswith(".") or file_path.name.startswith("~"):
            return False
        return True

    def _is_file_stable(self, file_path: Path, wait_time: int = 5) -> bool:
        try:
            if not file_path.exists():
                return False
            size1 = file_path.stat().st_size
            time.sleep(wait_time)
            if not file_path.exists():
                return False
            size2 = file_path.stat().st_size
            return size1 == size2
        except Exception as e:
            logger.warning("Error checking file stability: %s", e)
            return False

    # ──────────────────────────── file renaming ──────────────────────────────

    @staticmethod
    def _krisp_id_to_datetime(krisp_id: str) -> Optional[datetime]:
        """
        Extract datetime from Krisp UUID v7.
        First 48 bits (12 hex chars) = Unix timestamp in ms.
        Only works for newer IDs (019... range, ~2024-2026).
        """
        return krisp_id_to_datetime(krisp_id)

    @staticmethod
    def _sanitize_title(title: str, max_len: int = 80) -> str:
        """Clean up title for filesystem use."""
        return sanitize_title(title, max_len)

    def _rename_audio_file(self, file_path: Path) -> Path:
        """
        Rename audio file to YYMMDD_EventName_krispID.ext

        Naming logic:
        1. Extract date/time from Krisp UUID v7
        2. Look up event in CalDAV calendar by date/time
        3. If calendar returned a meaningful title → use it
        4. If not → keep original filename (Crisp title)
        5. If file is a bare hex ID → try to add at least a date prefix
        """
        # Already renamed (YYMMDD_ or YYYY-MM-DD_ prefix)
        if re.match(r"^(\d{4}-\d{2}-\d{2}_|\d{6}_)", file_path.name):
            return file_path

        stem = file_path.stem
        ext = file_path.suffix

        # Extract Krisp ID (full hex from filename)
        hex_match = re.match(r'^([0-9a-f]{16,})$', stem)
        full_hex = hex_match.group(1) if hex_match else None
        short_id = full_hex[:8] if full_hex else None

        # Extract date/time from UUID v7
        uuid_dt = self._krisp_id_to_datetime(full_hex) if full_hex else None

        # Determine meeting date
        if uuid_dt:
            meeting_date = uuid_dt.date()
            hour, minute = uuid_dt.hour, uuid_dt.minute
        else:
            stat = file_path.stat()
            try:
                creation_time = stat.st_birthtime
            except AttributeError:
                creation_time = stat.st_mtime
            meeting_date = datetime.fromtimestamp(creation_time).date()
            hour, minute = None, None

        # Search in CalDAV calendar
        calendar_title = None
        if hour is not None and self.config.caldav_url:
            try:
                calendar_title = find_event_name(
                    meeting_date=meeting_date,
                    meeting_hour=hour,
                    meeting_minute=minute,
                    caldav_url=self.config.caldav_url,
                    caldav_username=self.config.caldav_username,
                    caldav_password=self.config.caldav_password,
                    calendar_name=self.config.caldav_calendar_name,
                )
            except Exception as e:
                logger.warning("Calendar lookup failed: %s", e)

        # Choose best title
        if calendar_title and calendar_title.strip().lower() not in ("busy",):
            title = calendar_title
            logger.info("Calendar: '%s'", title)
        elif not full_hex:
            # Filename contains Crisp title (not bare hex) — use as-is
            title = stem
        else:
            title = None  # Raw hex without calendar name

        # Build new filename
        date_prefix = meeting_date.strftime("%y%m%d")
        safe_title = self._sanitize_title(title) if title else None

        if safe_title and short_id:
            new_name = f"{date_prefix}_{safe_title}_{short_id}{ext}"
        elif safe_title:
            new_name = f"{date_prefix}_{safe_title}{ext}"
        elif short_id:
            new_name = f"{date_prefix}_{short_id}{ext}"
        else:
            new_name = f"{date_prefix}_{stem}{ext}"

        new_path = file_path.parent / new_name

        if new_path.exists() and new_path != file_path:
            logger.warning("File %s already exists, skipping rename", new_name)
            return new_path

        file_path.rename(new_path)
        logger.info("Renamed: %s -> %s", file_path.name, new_name)
        return new_path

    # ──────────────────────────── processed detection ────────────────────────

    def _is_file_already_processed(self, file_path: Path) -> bool:
        """Check if _transcription.txt + _summary.md already exist for this file."""
        audio_dir = file_path.parent
        stem = file_path.stem

        # Check by current name
        if self._has_outputs(audio_dir, stem):
            return True

        # Check by date-prefixed name (if not yet renamed)
        if not re.match(r"^(\d{4}-\d{2}-\d{2}_|\d{6}_)", file_path.name):
            stat = file_path.stat()
            try:
                creation_time = stat.st_birthtime
            except AttributeError:
                creation_time = stat.st_mtime
            date_str = datetime.fromtimestamp(creation_time).strftime("%Y-%m-%d")
            dated_stem = f"{date_str}_{stem}"
            if self._has_outputs(audio_dir, dated_stem):
                return True

        return False

    @staticmethod
    def _has_outputs(directory: Path, stem: str) -> bool:
        trans = directory / f"{stem}_transcription.txt"
        summ = directory / f"{stem}_summary.md"
        return trans.exists() and summ.exists()

    # ──────────────────────────── main pipeline ──────────────────────────────

    def process_file(self, file_path: Path):
        """Full audio file processing pipeline."""
        file_str = str(file_path)

        if file_str in self.processing_lock:
            logger.info("File %s is already being processed", file_path.name)
            return
        if file_str in self.processed_files:
            logger.info("File %s was already processed", file_path.name)
            return

        self.processing_lock[file_str] = True

        try:
            logger.info("Starting file processing: %s", file_path.name)

            # Step 1: Rename
            logger.info("Step 1: Renaming file...")
            file_path = self._rename_audio_file(file_path)

            # Step 2: Transcribe
            logger.info("Step 2: Transcribing audio...")
            transcription = transcribe_audio(str(file_path), self.config)

            if not transcription:
                logger.error("Failed to get transcription for %s", file_path.name)
                return

            transcription_file = self._save_transcription(file_path, transcription)

            # Step 3: Summarize + classify action items
            logger.info("Step 3: Summarization and action item classification...")
            parent_tasks = self._get_inbox_parent_tasks()
            summary = summarize_transcription(transcription, self.config, parent_tasks)

            if not summary:
                logger.error("Failed to create summary for %s", file_path.name)
                return

            summary_file = self._save_summary(file_path, summary, transcription_file)

            logger.info("File %s processed successfully!", file_path.name)
            logger.info("   Transcription: %s", transcription_file)
            logger.info("   Summary: %s", summary_file)

            # Step 4: Add action items to inbox + YouTrack
            if self.config.inbox_enabled:
                self._send_to_inbox_and_youtrack(summary, file_path)

            self.processed_files.add(file_str)
            # Update lock key for new filename
            self.processed_files.add(str(file_path))

        except Exception as e:
            logger.error("Error processing file %s: %s", file_path.name, e, exc_info=True)
        finally:
            if file_str in self.processing_lock:
                del self.processing_lock[file_str]

    # ──────────────────────────── save outputs ───────────────────────────────

    @staticmethod
    def _save_transcription(audio_file: Path, transcription: str) -> Path:
        """Save transcription alongside audio file."""
        transcription_file = audio_file.parent / f"{audio_file.stem}_transcription.txt"
        transcription_file.write_text(transcription, encoding="utf-8")
        return transcription_file

    def _save_summary(self, audio_file: Path, summary: Dict, transcription_file: Path) -> Path:
        """Save summary as Markdown alongside audio file."""
        # Support both date formats: YYYY-MM-DD and YYMMDD
        raw_date = audio_file.stem.split("_")[0] if "_" in audio_file.stem else ""
        if re.match(r"^\d{6}$", raw_date):
            date_str = f"20{raw_date[:2]}-{raw_date[2:4]}-{raw_date[4:6]}"
        elif re.match(r"^\d{4}-\d{2}-\d{2}$", raw_date):
            date_str = raw_date
        else:
            date_str = datetime.now().strftime("%Y-%m-%d")

        summary_data = {
            "audio_file": str(audio_file),
            "transcription_file": str(transcription_file),
            "date": date_str,
            **summary,
        }

        md_file = audio_file.parent / f"{audio_file.stem}_summary.md"
        md_content = self._format_summary_markdown(summary_data)
        md_file.write_text(md_content, encoding="utf-8")
        return md_file

    @staticmethod
    def _format_summary_markdown(summary_data: Dict) -> str:
        """Format summary as Markdown."""
        lines = [
            "# Call Summary",
            "",
            f"**Date:** {summary_data.get('date', 'N/A')}",
            f"**Audio file:** `{Path(summary_data.get('audio_file', '')).name}`",
            "",
            "## Project",
            summary_data.get("project", "Not specified"),
            "",
            "## Brief Summary",
            summary_data.get("summary", "N/A"),
            "",
            "## Main Discussion Topics",
        ]

        topics = summary_data.get("topics", [])
        if topics:
            for i, topic in enumerate(topics, 1):
                if isinstance(topic, dict):
                    title = topic.get("title", "Not specified")
                    what_discussed = topic.get("what_discussed", "")
                    why_discussed = topic.get("why_discussed", "")
                    decisions = topic.get("decisions", "")
                    key_points = topic.get("key_points", [])

                    lines.append(f"\n### {i}. {title}")
                    if what_discussed and what_discussed != "Not specified":
                        lines.append(f"**What was discussed:** {what_discussed}")
                    if why_discussed and why_discussed != "Not specified":
                        lines.append(f"**Why it was discussed:** {why_discussed}")
                    if decisions and decisions != "Not specified":
                        lines.append(f"**Decisions made:** {decisions}")
                    if key_points:
                        lines.append("**Key points:**")
                        for point in key_points:
                            lines.append(f"  - {point}")
                else:
                    lines.append(f"- {topic}")
        else:
            lines.append("- Not specified")

        lines.extend(["", "## Action Items"])

        action_items = summary_data.get("action_items", [])
        if action_items:
            for i, item in enumerate(action_items, 1):
                assignee = item.get("assignee", "Unassigned")
                description = item.get("description", "N/A")
                due_date = item.get("due_date", "Not specified")
                parent = item.get("parent_task", "")
                lines.append(f"{i}. **{description}**")
                lines.append(f"   - Assignee: {assignee}")
                lines.append(f"   - Due date: {due_date}")
                if parent:
                    lines.append(f"   - Parent task: {parent}")
        else:
            lines.append("- No action items found")

        participants = summary_data.get("participants", [])
        if participants:
            lines.extend(["", "## Participants", ", ".join(participants)])

        return "\n".join(lines)

    # ──────────────────────────── inbox integration ──────────────────────────

    def _get_inbox_parent_tasks(self) -> List[str]:
        """Return list of parent task titles from inbox."""
        if not self.config.inbox_enabled:
            return []
        mgr = InboxManager(self.config.inbox_file)
        return mgr.get_parent_task_titles()

    def _send_to_inbox_and_youtrack(self, summary: Dict, file_path: Path):
        """
        Create YT subtasks for action items, then insert them into the inbox.
        Also posts the call summary as a comment on parent YT tasks.
        """
        action_items = summary.get("action_items", [])
        if not action_items:
            logger.info("No action items to add")
            return

        mgr = InboxManager(self.config.inbox_file)
        roots = mgr.parse_tree()
        parent_map = {r.title: r for r in roots}

        # Prepare classified items
        classified = []
        for item in action_items:
            classified.append({
                "description": item.get("description", ""),
                "parent_task": item.get("parent_task", ""),
                "due_date": item.get("due_date", ""),
            })

        # Step 5: Create YouTrack subtasks (before inbox insert so we can attach YT link immediately)
        if self.config.youtrack_enabled and self.config.youtrack_token:
            self._create_youtrack_tasks(classified, parent_map, summary, file_path)

        # Insert into inbox (with YT URL if created)
        mgr.insert_tasks(classified)
        logger.info("Added %d action items to inbox", len(classified))

    def _create_youtrack_tasks(
        self,
        classified: List[Dict],
        parent_map: Dict[str, "InboxManager"],
        summary: Dict,
        file_path: Path,
    ):
        """Create YT subtasks and post summary as comment."""
        yt = YouTrackClient(
            self.config.youtrack_url,
            self.config.youtrack_token,
            self.config.youtrack_project,
        )

        # Call date from filename
        date_str = (
            file_path.stem.split("_")[0]
            if "_" in file_path.stem
            else datetime.now().strftime("%Y-%m-%d")
        )

        # Track parents with new items (for posting summary comment)
        parents_with_new_items = set()

        for item in classified:
            parent_title = item.get("parent_task", "").strip()

            # New parents (__NEW__:...) don't have YT tasks yet
            if parent_title.startswith("__NEW__:"):
                continue

            parent = parent_map.get(parent_title)
            if not parent or not parent.youtrack_id:
                continue

            # Create subtask: "Parent // Child"
            yt_summary = f"{parent_title} // {item['description']}"
            yt_description = f"From call on {date_str}"

            child_id = yt.create_subtask(parent.youtrack_id, yt_summary, yt_description)
            if child_id:
                item["youtrack_url"] = yt.issue_url(child_id)
                parents_with_new_items.add(parent_title)
                logger.info(
                    "Created YT subtask %s: %s", child_id, item["description"][:60]
                )

        # Post call summary as comment on parent YT tasks
        if parents_with_new_items:
            comment_text = yt.format_summary_comment(summary)
            for parent_title in parents_with_new_items:
                parent = parent_map[parent_title]
                if parent.youtrack_id:
                    yt.add_comment(parent.youtrack_id, comment_text)
                    logger.info(
                        "Comment on %s (%s)", parent.youtrack_id, parent_title[:40]
                    )

    # ──────────────────────────── scan existing ──────────────────────────────

    def get_unprocessed_files(self, watch_dir: Path) -> List[Path]:
        """Return list of unprocessed audio files."""
        if not watch_dir.exists():
            return []
        files = []
        for file_path in sorted(watch_dir.iterdir()):
            if not file_path.is_file():
                continue
            if not self._is_audio_file(file_path):
                continue
            if not self._is_file_already_processed(file_path):
                files.append(file_path)
        return files

    def scan_existing_files(self, watch_dir: Path):
        """Scan existing files and process new ones."""
        logger.info("Scanning existing files in directory...")
        if not watch_dir.exists():
            logger.warning("Directory %s does not exist", watch_dir)
            return

        existing_files = self.get_unprocessed_files(watch_dir)
        if not existing_files:
            logger.info("No new files to process")
            return

        logger.info("Found %d file(s) to process:", len(existing_files))
        for file_path in existing_files:
            logger.info("  - %s", file_path.name)

        for file_path in existing_files:
            logger.info("Processing existing file: %s", file_path.name)
            self.process_file(file_path)


def main():
    """Main function — monitoring mode (watchdog)."""
    logger.info("Starting call processing system...")

    config = Config()

    watch_dir = Path(config.watch_dir)
    if not watch_dir.exists():
        logger.warning("Directory %s does not exist. Creating...", watch_dir)
        watch_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Monitoring directory: %s", watch_dir)

    event_handler = AudioFileHandler(config)

    # Process existing files on startup
    logger.info("")
    logger.info("=" * 60)
    logger.info("STEP 1: Processing existing files")
    logger.info("=" * 60)
    event_handler.scan_existing_files(watch_dir)
    logger.info("")

    # Monitor new files
    logger.info("=" * 60)
    logger.info("STEP 2: Starting new file monitoring")
    logger.info("=" * 60)
    observer = Observer()
    observer.schedule(event_handler, str(watch_dir), recursive=False)

    observer.start()
    logger.info("Monitoring system started. Waiting for new files...")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stop signal received. Shutting down...")
        observer.stop()

    observer.join()
    logger.info("Shutdown complete.")


if __name__ == "__main__":
    main()
