"""UUID v7 timestamp extraction for Krisp recording IDs.

Krisp uses UUIDv7 for recording IDs. The first 48 bits (12 hex chars)
encode a Unix timestamp in milliseconds — giving us the exact date and
time the recording was created.
"""

from datetime import datetime, timezone
from typing import Optional


def krisp_id_to_datetime(krisp_id: str) -> Optional[datetime]:
    """Extract local datetime from a Krisp UUID v7 recording ID.

    Args:
        krisp_id: Full or partial hex ID (at least 8 chars).

    Returns:
        Local datetime or None if the ID doesn't encode a valid timestamp.
    """
    hex_str = krisp_id.replace("-", "")
    if len(hex_str) < 12:
        hex_str = hex_str + "0" * (12 - len(hex_str))

    try:
        ts_ms = int(hex_str[:12], 16)
        local_tz = datetime.now(timezone.utc).astimezone().tzinfo
        dt_utc = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        dt_local = dt_utc.astimezone(local_tz)
        if 2024 <= dt_local.year <= 2027:
            return dt_local
    except (ValueError, OSError, OverflowError):
        pass
    return None
