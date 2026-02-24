"""
Search for a calendar event name by recording date and time via CalDAV.

Used to name Krisp recordings using the actual meeting title from the calendar.

Note: Krisp shows time in the system's local timezone (detected automatically).
Calendar events are stored with tzinfo. All comparisons are done in UTC.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import caldav

logger = logging.getLogger(__name__)

# Detect system local timezone (Krisp records time in local timezone)
_LOCAL_TZ = datetime.now(timezone.utc).astimezone().tzinfo


def find_event_name(
    meeting_date: date,
    meeting_hour: int,
    meeting_minute: int,
    caldav_url: str,
    caldav_username: str,
    caldav_password: str,
    calendar_name: str = "",
) -> Optional[str]:
    """
    Search for a CalDAV calendar event matching the given date and time.

    Args:
        meeting_date: Meeting date
        meeting_hour: Start hour (0-23) in local timezone
        meeting_minute: Start minute (0-59)
        caldav_url: CalDAV server URL
        caldav_username: Username
        caldav_password: Password
        calendar_name: Calendar name (default "My Events")

    Returns:
        Event title or None if not found.
    """
    if not caldav_url or not caldav_username or not caldav_password:
        logger.warning("CalDAV credentials not configured, skipping calendar lookup")
        return None

    try:
        client = caldav.DAVClient(
            url=caldav_url,
            username=caldav_username,
            password=caldav_password,
        )
        principal = client.principal()
        calendars = principal.calendars()

        # Find target calendar by name
        target_cal = None
        for cal in calendars:
            if cal.name == calendar_name:
                target_cal = cal
                break

        if target_cal is None:
            logger.warning("Calendar '%s' not found", calendar_name)
            return None

        # Search for events on this day
        day_start = datetime(meeting_date.year, meeting_date.month, meeting_date.day)
        day_end = day_start + timedelta(days=1)
        events = target_cal.date_search(start=day_start, end=day_end)

        # Krisp recording time is in local timezone, convert to UTC
        meeting_local = datetime(
            meeting_date.year, meeting_date.month, meeting_date.day,
            meeting_hour, meeting_minute,
            tzinfo=_LOCAL_TZ,
        )
        meeting_utc = meeting_local.astimezone(timezone.utc)

        candidates = []
        for event in events:
            try:
                comp = event.icalendar_component
                dtstart = comp.get("DTSTART")
                dtend = comp.get("DTEND")
                summary = comp.get("SUMMARY")

                if not dtstart or not summary:
                    continue

                start_dt = dtstart.dt
                # Skip all-day events
                if isinstance(start_dt, date) and not isinstance(start_dt, datetime):
                    continue

                # Convert to UTC for uniform comparison
                if start_dt.tzinfo:
                    start_utc = start_dt.astimezone(timezone.utc)
                else:
                    # Naive datetime — treat as local timezone
                    start_utc = start_dt.replace(tzinfo=_LOCAL_TZ).astimezone(timezone.utc)

                if dtend:
                    end_dt = dtend.dt
                    if end_dt.tzinfo:
                        end_utc = end_dt.astimezone(timezone.utc)
                    else:
                        end_utc = end_dt.replace(tzinfo=_LOCAL_TZ).astimezone(timezone.utc)
                else:
                    end_utc = start_utc + timedelta(hours=1)

                # Check if recording time falls within event interval (±15 min)
                overlap_start = start_utc - timedelta(minutes=15)
                overlap_end = end_utc + timedelta(minutes=15)

                if overlap_start <= meeting_utc <= overlap_end:
                    # Determine user participation status
                    partstat = _get_user_partstat(comp, caldav_username)
                    candidates.append({
                        "title": str(summary),
                        "partstat": partstat,
                        "start_utc": start_utc,
                    })

            except Exception as e:
                logger.debug("Error parsing event: %s", e)
                continue

        if not candidates:
            logger.info("No calendar events found for %s %02d:%02d (local)", meeting_date, meeting_hour, meeting_minute)
            return None

        # If multiple candidates, prefer ACCEPTED
        accepted = [c for c in candidates if c["partstat"] == "ACCEPTED"]
        if accepted:
            chosen = min(accepted, key=lambda c: abs((c["start_utc"] - meeting_utc).total_seconds()))
        else:
            chosen = min(candidates, key=lambda c: abs((c["start_utc"] - meeting_utc).total_seconds()))

        logger.info("Calendar event found: '%s' (partstat=%s)", chosen["title"], chosen["partstat"])
        return chosen["title"]

    except Exception as e:
        logger.error("Calendar lookup failed: %s", e)
        return None


def _get_user_partstat(ical_component, username: str) -> str:
    """
    Determine the user's PARTSTAT for a calendar event.

    - If the user is found in ATTENDEE → return their PARTSTAT
    - If the user is the organizer or there are no attendees → "ACCEPTED" (assumed)
    """
    attendee_list = ical_component.get("ATTENDEE", [])
    if not isinstance(attendee_list, list):
        attendee_list = [attendee_list]

    if not attendee_list:
        # Personal event without attendees — assume presence
        return "ACCEPTED"

    for attendee in attendee_list:
        try:
            email = str(attendee).replace("mailto:", "").strip().lower()
            if username.lower() in email:
                if hasattr(attendee, "params"):
                    partstat_values = attendee.params.get("PARTSTAT", ["NEEDS-ACTION"])
                    if isinstance(partstat_values, list):
                        return partstat_values[0]
                    return str(partstat_values)
                return "NEEDS-ACTION"
        except Exception:
            continue

    # User not in attendee list — might be organizer
    organizer = ical_component.get("ORGANIZER")
    if organizer:
        org_email = str(organizer).replace("mailto:", "").strip().lower()
        if username.lower() in org_email:
            return "ACCEPTED"

    # User not found — treat as unknown
    return "UNKNOWN"
