#!/usr/bin/env python3
"""
Rename downloaded files using:
1. Timestamp from Krisp UUID v7 (date + meeting time)
2. krisp_id → title mapping from download log
3. CalDAV calendar for date/time matching
4. Krisp title as fallback

Output format: YYMMDD_EventName_krispID.ext

Usage:
    python rename_downloaded.py              # dry-run
    python rename_downloaded.py --apply      # apply renames
"""

import json
import os
import re
import sys
import logging
from pathlib import Path
from datetime import datetime, date, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from pipeline.calendar import find_event_name
from utils.krisp_id import krisp_id_to_datetime
from utils.sanitize import sanitize_title

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

MEETINGS_DIR = Path(os.getenv(
    "MEETINGS_DIR",
    str(Path(__file__).parent.parent / "meetings")
))
AUDIO_EXTS = {".mp3", ".mp4", ".m4a", ".wav", ".mov"}
MAPPING_FILE = Path("/tmp/crisp_title_mapping.json")

# Local timezone for converting UUID timestamps
_LOCAL_TZ = datetime.now(timezone.utc).astimezone().tzinfo


def load_mapping() -> dict:
    """Load krisp_id → metadata mapping from JSON."""
    if not MAPPING_FILE.exists():
        logger.error(f"Mapping file not found: {MAPPING_FILE}")
        return {}
    with open(MAPPING_FILE) as f:
        return json.load(f)


def parse_crisp_title(title: str) -> dict:
    """
    Parse Crisp title to extract time and meeting date.

    Formats:
        "10:22 PM - Arc meeting January 15"  → hour=22, minute=22, date=Jan 15
        "Company x Partner" → no time, just title
    """
    result = {"title": title, "hour": None, "minute": None, "meeting_date": None}

    m = re.match(r'^(\d{1,2}):(\d{2})\s+(AM|PM)\s+-\s+(.+)$', title, re.IGNORECASE)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2))
        ampm = m.group(3).upper()
        rest_title = m.group(4).strip()

        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0

        result["hour"] = hour
        result["minute"] = minute
        result["title"] = rest_title

        date_match = re.search(
            r'(?:meeting|Discord)\s+(\w+)\s+(\d{1,2})',
            rest_title, re.IGNORECASE
        )
        if date_match:
            month_name = date_match.group(1)
            day = int(date_match.group(2))
            for year in [2026, 2025]:
                try:
                    result["meeting_date"] = datetime.strptime(
                        f"{month_name} {day} {year}", "%B %d %Y"
                    ).date()
                    break
                except ValueError:
                    continue

    return result


def find_related_files(audio_path: Path) -> list:
    """Find _transcription.txt and _summary.md for an audio file."""
    stem = audio_path.stem
    parent = audio_path.parent
    related = []
    for suffix in ("_transcription.txt", "_summary.md"):
        f = parent / f"{stem}{suffix}"
        if f.exists():
            related.append(f)
    return related


def main():
    apply = "--apply" in sys.argv

    if apply:
        logger.info("=== APPLYING RENAMES ===\n")
    else:
        logger.info("=== DRY RUN (use --apply to rename) ===\n")

    mapping = load_mapping()
    if not mapping:
        logger.error("No mapping data")
        return

    caldav_url = os.getenv("CALDAV_URL", "")
    caldav_user = os.getenv("CALDAV_USERNAME", "")
    caldav_pass = os.getenv("CALDAV_PASSWORD", "")
    caldav_cal = os.getenv("CALDAV_CALENDAR_NAME", "")

    audio_files = sorted(
        f for f in MEETINGS_DIR.iterdir()
        if f.is_file() and f.suffix.lower() in AUDIO_EXTS
    )

    rename_count = 0
    skip_count = 0
    calendar_hits = 0
    calendar_misses = 0

    for audio_file in audio_files:
        stem = audio_file.stem
        ext = audio_file.suffix

        # Extract krisp ID from filename
        # Format: 2026-02-22_{hexID} or just {hexID}
        m = re.match(r'^(\d{4}-\d{2}-\d{2})_([0-9a-f]{16,})$', stem)
        if m:
            full_hex = m.group(2)
            short_id = full_hex[:8]
        else:
            m = re.match(r'^([0-9a-f]{16,})$', stem)
            if m:
                full_hex = m.group(1)
                short_id = full_hex[:8]
            else:
                # Already has a proper name (YYMMDD_Title_id or similar)
                skip_count += 1
                continue

        # Look up in mapping
        meta = mapping.get(short_id)
        if not meta:
            skip_count += 1
            continue

        crisp_title = meta["title"]
        parsed = parse_crisp_title(crisp_title)

        # Extract date+time from Krisp UUID v7 (most reliable source for date)
        uuid_dt = krisp_id_to_datetime(full_hex)

        # Determine meeting date — priority: UUID date > title date > file date
        # UUID is authoritative because title parsing can't reliably determine year
        if uuid_dt:
            meeting_date = uuid_dt.date()
        elif parsed["meeting_date"]:
            meeting_date = parsed["meeting_date"]
        else:
            try:
                ctime = audio_file.stat().st_birthtime
            except AttributeError:
                ctime = audio_file.stat().st_mtime
            meeting_date = datetime.fromtimestamp(ctime).date()

        # Determine hour/minute — priority: title time > UUID time
        hour = parsed["hour"]
        minute = parsed["minute"]
        if hour is None and uuid_dt:
            hour = uuid_dt.hour
            minute = uuid_dt.minute

        # Try calendar lookup (now works for ALL entries with UUID timestamps)
        calendar_title = None
        if hour is not None and meeting_date:
            calendar_title = find_event_name(
                meeting_date=meeting_date,
                meeting_hour=hour,
                meeting_minute=minute,
                caldav_url=caldav_url,
                caldav_username=caldav_user,
                caldav_password=caldav_pass,
                calendar_name=caldav_cal,
            )
            if calendar_title:
                calendar_hits += 1
            else:
                calendar_misses += 1

        # Choose best title
        if calendar_title and calendar_title.strip().lower() not in ("busy",):
            final_title = calendar_title
            source = "calendar"
        else:
            final_title = parsed["title"]
            source = "crisp"

        safe_title = sanitize_title(final_title)
        date_prefix = meeting_date.strftime("%y%m%d")
        new_name = f"{date_prefix}_{safe_title}_{short_id}{ext}"

        if new_name == audio_file.name:
            skip_count += 1
            continue

        new_path = audio_file.parent / new_name
        if new_path.exists() and new_path != audio_file:
            logger.info(f"  CONFLICT: {new_name} already exists, skipping")
            skip_count += 1
            continue

        # Collect all files to rename (audio + transcription + summary)
        old_stem = audio_file.stem
        new_stem = Path(new_name).stem
        files_to_rename = [(audio_file, new_path)]

        for related in find_related_files(audio_file):
            new_related_name = related.name.replace(old_stem, new_stem, 1)
            files_to_rename.append((related, related.parent / new_related_name))

        logger.info(f"[{source}] {audio_file.name}")
        for old, new in files_to_rename:
            logger.info(f"  -> {new.name}")
            if apply:
                old.rename(new)

        rename_count += 1

    logger.info(f"\nTotal: {rename_count} to rename, {skip_count} skipped")
    logger.info(f"Calendar: {calendar_hits} hits, {calendar_misses} misses")
    if not apply and rename_count > 0:
        logger.info("\nRun with --apply to execute renames.")


if __name__ == "__main__":
    main()
