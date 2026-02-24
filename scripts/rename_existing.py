#!/usr/bin/env python3
"""
Rename existing recordings using calendar event names.

For each audio file:
1. Extract date and time from filename
2. Look up event in CalDAV calendar
3. Rename audio + _transcription.txt + _summary.md

Usage:
    python rename_existing.py              # dry-run
    python rename_existing.py --apply      # apply renames
"""

import os
import re
import sys
import logging
from pathlib import Path
from datetime import datetime, date

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from pipeline.calendar import find_event_name

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

MEETINGS_DIR = Path(os.getenv(
    "MEETINGS_DIR",
    str(Path(__file__).parent.parent / "meetings")
))
AUDIO_EXTS = {".mp3", ".mp4", ".m4a", ".wav", ".mov"}


def parse_krisp_filename(name: str) -> dict | None:
    """
    Parse Krisp filename and extract meeting date/time.

    Formats:
      YYYY-MM-DD_HHMM AM/PM - <app> meeting <Month> <Day>_<krispID>.ext
      YYYY-MM-DD_<Title>_<krispID>.ext
      YYYY-MM-DD_<hexID>.ext
      <hexID>.ext
    """
    stem = Path(name).stem
    ext = Path(name).suffix

    # Format: YYYY-MM-DD_HHMM AP - <App> meeting <Month> <Day>_krispID
    m = re.match(
        r'^(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})\s+(AM|PM)\s+-\s+(.+?)_([0-9a-f]{8})$',
        stem, re.IGNORECASE
    )
    if m:
        download_date = m.group(1)
        hour, minute, ampm = int(m.group(2)), int(m.group(3)), m.group(4).upper()
        krisp_title = m.group(5).strip()
        krisp_id = m.group(6)

        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0

        # Extract actual meeting date from title ("Arc meeting February 6")
        year = int(download_date[:4])
        meeting_date = None
        date_match = re.search(r'(?:meeting|Discord)\s+(\w+)\s+(\d{1,2})', krisp_title, re.IGNORECASE)
        if date_match:
            month_name, day = date_match.group(1), int(date_match.group(2))
            try:
                meeting_date = datetime.strptime(f"{month_name} {day} {year}", "%B %d %Y").date()
            except ValueError:
                pass

        if not meeting_date:
            meeting_date = datetime.strptime(download_date, "%Y-%m-%d").date()

        return {
            "meeting_date": meeting_date,
            "hour": hour,
            "minute": minute,
            "krisp_id": krisp_id,
            "ext": ext,
            "has_krisp_title": False,
            "krisp_title": krisp_title,  # "Arc meeting February 6"
        }

    # Format: YYYY-MM-DD_<fullHexID>.ext (hex-only, no title)
    m = re.match(r'^(\d{4}-\d{2}-\d{2})_([0-9a-f]{16,})$', stem)
    if m:
        file_date = m.group(1)
        full_hex = m.group(2)
        return {
            "meeting_date": datetime.strptime(file_date, "%Y-%m-%d").date(),
            "hour": None,
            "minute": None,
            "krisp_id": full_hex[:8],
            "ext": ext,
            "has_krisp_title": False,
            "krisp_title": None,
        }

    # Format: YYYY-MM-DD_<Title>_<krispID>
    m = re.match(r'^(\d{4}-\d{2}-\d{2})_(.+)_([0-9a-f]{8})$', stem)
    if m:
        file_date = m.group(1)
        title = m.group(2)
        krisp_id = m.group(3)

        # If title is already in non-Latin chars — it's meaningful
        if re.search(r'[а-яА-Я]', title):
            return {
                "meeting_date": datetime.strptime(file_date, "%Y-%m-%d").date(),
                "hour": None,
                "minute": None,
                "krisp_id": krisp_id,
                "ext": ext,
                "has_krisp_title": True,
                "existing_title": title,
            }

    # Format: YYYY-MM-DD_<Title>.ext (manually named, no krispID)
    m = re.match(r'^(\d{4}-\d{2}-\d{2})_(.+)$', stem)
    if m:
        file_date = m.group(1)
        title = m.group(2)
        return {
            "meeting_date": datetime.strptime(file_date, "%Y-%m-%d").date(),
            "hour": None,
            "minute": None,
            "krisp_id": None,
            "ext": ext,
            "has_krisp_title": True,
            "existing_title": title,
        }

    return None


def find_related_files(audio_path: Path) -> list[Path]:
    """Find _transcription.txt and _summary.md for an audio file."""
    stem = audio_path.stem
    parent = audio_path.parent
    related = []
    for suffix in ("_transcription.txt", "_summary.md", "_transcription_SMALL.txt"):
        f = parent / f"{stem}{suffix}"
        if f.exists():
            related.append(f)
    return related


def build_new_name(info: dict, calendar_title: str | None) -> str | None:
    """Build new filename: YYMMDD_EventName_krispID.ext"""
    meeting_date = info["meeting_date"]
    if not meeting_date:
        return None

    date_prefix = meeting_date.strftime("%y%m%d")
    krisp_id = info["krisp_id"]
    ext = info["ext"]

    if calendar_title:
        # Skip uninformative calendar titles
        if calendar_title.strip().lower() in ("busy",):
            if info.get("has_krisp_title") and info.get("existing_title"):
                title = info["existing_title"]
            elif info.get("krisp_title"):
                title = info["krisp_title"]
            else:
                return None
        else:
            title = calendar_title
    elif info.get("has_krisp_title") and info.get("existing_title"):
        title = info["existing_title"]
    elif info.get("krisp_title"):
        title = info["krisp_title"]
    elif krisp_id:
        # Hex-only file without title — just reformat date
        return f"{date_prefix}_{krisp_id}{ext}"
    else:
        return None  # nothing to change, no meaningful title

    # Sanitize: remove only filesystem-unsafe characters
    import unicodedata
    title = unicodedata.normalize("NFC", title)  # macOS NFD → NFC
    safe_title = "".join(
        c for c in title
        if c not in '/\\:*?"<>|'
    ).strip()
    safe_title = safe_title[:80]

    if krisp_id:
        return f"{date_prefix}_{safe_title}_{krisp_id}{ext}"
    else:
        return f"{date_prefix}_{safe_title}{ext}"


def main():
    apply = "--apply" in sys.argv

    if apply:
        logger.info("=== APPLYING RENAMES ===\n")
    else:
        logger.info("=== DRY RUN (use --apply to rename) ===\n")

    caldav_url = os.getenv("CALDAV_URL", "")
    caldav_user = os.getenv("CALDAV_USERNAME", "")
    caldav_pass = os.getenv("CALDAV_PASSWORD", "")
    caldav_cal = os.getenv("CALDAV_CALENDAR_NAME", "")

    # Collect unique krispID → best file (with transcripts)
    audio_files = sorted(
        f for f in MEETINGS_DIR.iterdir()
        if f.is_file() and f.suffix.lower() in AUDIO_EXTS
    )

    # Group by krispID (or by filename if no ID)
    seen_ids = {}
    for f in audio_files:
        info = parse_krisp_filename(f.name)
        if not info:
            continue
        kid = info["krisp_id"] or f.stem  # for files without krispID — use stem as key
        has_outputs = bool(find_related_files(f))
        # Prefer file with outputs
        if kid not in seen_ids or (has_outputs and not seen_ids[kid][1]):
            seen_ids[kid] = (f, has_outputs, info)

    rename_count = 0
    skip_count = 0
    fail_count = 0

    for key, (audio_file, has_outputs, info) in sorted(seen_ids.items(), key=lambda x: x[1][0].name):
        # Look up in calendar
        calendar_title = None
        if info["hour"] is not None and info["meeting_date"]:
            calendar_title = find_event_name(
                meeting_date=info["meeting_date"],
                meeting_hour=info["hour"],
                meeting_minute=info["minute"],
                caldav_url=caldav_url,
                caldav_username=caldav_user,
                caldav_password=caldav_pass,
                calendar_name=caldav_cal,
            )

        new_name = build_new_name(info, calendar_title)
        if not new_name or new_name == audio_file.name:
            skip_count += 1
            continue

        new_audio = audio_file.parent / new_name
        if new_audio.exists() and new_audio != audio_file:
            logger.info(f"  CONFLICT: {new_name} already exists, skipping")
            fail_count += 1
            continue

        # Collect all files to rename
        old_stem = audio_file.stem
        new_stem = Path(new_name).stem
        files_to_rename = [(audio_file, new_audio)]

        for related in find_related_files(audio_file):
            new_related_name = related.name.replace(old_stem, new_stem, 1)
            files_to_rename.append((related, related.parent / new_related_name))

        # Show / apply
        source_label = "calendar" if calendar_title else "existing"
        logger.info(f"[{source_label}] {audio_file.name}")
        for old, new in files_to_rename:
            logger.info(f"  -> {new.name}")
            if apply:
                old.rename(new)

        # Also rename duplicates (same krispID, different download dates) — skip deletion
        # No deletion — just rename the primary file

        rename_count += 1
        logger.info("")

    logger.info(f"\nTotal: {rename_count} renamed, {skip_count} skipped, {fail_count} conflicts")
    if not apply and rename_count > 0:
        logger.info("\nRun with --apply to execute renames.")


if __name__ == "__main__":
    main()
