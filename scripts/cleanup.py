#!/usr/bin/env python3
"""
Clean up duplicates in meetings directory.

For each krisp ID with multiple file versions:
- Keeps the new-format YYMMDD_ version (with meaningful name)
- Deletes old versions (YYYY-MM-DD_ or hex-only)
- Also deletes related _transcription.txt and _summary.md of duplicates

Usage:
    python cleanup.py              # dry-run
    python cleanup.py --apply      # delete duplicates
"""

import os
import re
import sys
import logging
from pathlib import Path
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

MEETINGS_DIR = Path(os.getenv(
    "MEETINGS_DIR",
    str(Path(__file__).parent.parent / "meetings")
))
AUDIO_EXTS = {".mp3", ".mp4", ".m4a", ".wav", ".mov"}


def extract_krisp_id(filename: str) -> str | None:
    """Extract 8-char krisp ID from filename."""
    # Look for _XXXXXXXX before extension or suffix like _transcription
    m = re.search(r'_([0-9a-f]{8})(?:[0-9a-f]*)?(?:_(?:transcription|summary))?(?:_SMALL)?\.\w+$', filename)
    if m:
        return m.group(1)
    # hex-only files: 019c32f92a3e74b3ad8cb19a13ae787e.mp3
    stem = Path(filename).stem
    if re.match(r'^[0-9a-f]{16,}$', stem):
        return stem[:8]
    return None


def classify_naming(filename: str) -> str:
    """Classify file naming format."""
    if re.match(r'^\d{6}_', filename):
        return "new"  # YYMMDD_Name_id.ext
    if re.match(r'^\d{4}-\d{2}-\d{2}_', filename):
        return "old"  # YYYY-MM-DD_...
    if re.match(r'^[0-9a-f]{16,}', filename):
        return "hex"  # bare hex ID
    return "other"


def get_file_group(filepath: Path) -> list[Path]:
    """Return file + all related files (transcription, summary)."""
    stem = filepath.stem
    parent = filepath.parent
    group = [filepath]
    for suffix in ("_transcription.txt", "_summary.md", "_transcription_SMALL.txt"):
        related = parent / f"{stem}{suffix}"
        if related.exists():
            group.append(related)
    return group


def main():
    apply = "--apply" in sys.argv

    if apply:
        logger.info("=== APPLYING CLEANUP ===\n")
    else:
        logger.info("=== DRY RUN (use --apply to delete) ===\n")

    # Group all audio files by krisp ID
    groups: dict[str, list[Path]] = defaultdict(list)
    for f in sorted(MEETINGS_DIR.iterdir()):
        if f.is_file() and f.suffix.lower() in AUDIO_EXTS:
            kid = extract_krisp_id(f.name)
            if kid:
                groups[kid].append(f)

    delete_count = 0
    keep_count = 0
    freed_bytes = 0

    for kid, audio_files in sorted(groups.items()):
        if len(audio_files) <= 1:
            continue  # no duplicates

        # Priority: new > old > hex > other
        priority = {"new": 0, "old": 1, "hex": 2, "other": 3}
        classified = [(f, classify_naming(f.name)) for f in audio_files]
        classified.sort(key=lambda x: (priority.get(x[1], 99), -len(x[0].stem)))

        keeper = classified[0][0]
        keeper_fmt = classified[0][1]
        duplicates = [f for f, _ in classified[1:]]

        logger.info(f"[{kid}] Keep ({keeper_fmt}): {keeper.name}")

        for dup in duplicates:
            dup_fmt = classify_naming(dup.name)
            dup_group = get_file_group(dup)
            for df in dup_group:
                size = df.stat().st_size
                freed_bytes += size
                logger.info(f"  DELETE ({dup_fmt}): {df.name}  [{size // 1024} KB]")
                if apply:
                    df.unlink()
                delete_count += 1
        keep_count += 1
        logger.info("")

    freed_mb = freed_bytes / (1024 * 1024)
    logger.info(f"\nSummary: {delete_count} files to delete, {keep_count} krisp IDs with duplicates")
    logger.info(f"Space freed: {freed_mb:.1f} MB")
    if not apply and delete_count > 0:
        logger.info("\nRun with --apply to execute deletions.")


if __name__ == "__main__":
    main()
