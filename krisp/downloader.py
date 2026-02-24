#!/usr/bin/env python3
"""
Fully automated Krisp recording downloader via Playwright.

Uses headless browser for:
1. Automatic login (using saved cookies)
2. Discovering new recordings
3. Automatic downloading
"""

import os
import re
import sys
import time
import subprocess
import logging
from pathlib import Path
from datetime import datetime, date
from typing import List, Dict, Optional
from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext
from dotenv import load_dotenv

# Load .env from call-processing/
_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

# Add call-processing/ to sys.path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)


class KrispAutoDownloader:
    """Fully automated Krisp recording downloader."""

    def __init__(
        self,
        download_dir: Path,
        email: str,
        check_interval: int = 300,
        headless: bool = True,
    ):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)

        self.email = email
        self.check_interval = check_interval
        self.headless = headless

        self.downloaded_ids = self._load_downloaded_ids()
        self.state_dir = Path(__file__).parent / ".krisp_state"
        self.state_dir.mkdir(exist_ok=True)

    def _load_downloaded_ids(self) -> set:
        """Load list of already-downloaded recordings from state file + scan existing files.
        All IDs normalized to first 8 chars for comparison."""
        ids = set()
        # From state file (may contain full 32-char IDs)
        state_file = self.download_dir / ".krisp_downloaded_auto.txt"
        if state_file.exists():
            for line in state_file.read_text().strip().split("\n"):
                line = line.strip()
                if line:
                    ids.add(line[:8])  # normalize to 8 chars
        # From existing file names in folder
        for f in self.download_dir.iterdir():
            if f.suffix.lower() in ('.mp3', '.mp4', '.m4a', '.wav'):
                match = re.search(r'_([0-9a-f]{8})[0-9a-f]*\.\w+$', f.name)
                if match:
                    ids.add(match.group(1))
        return ids

    def _save_downloaded_id(self, recording_id: str):
        """Save downloaded recording ID (first 8 chars)."""
        short_id = recording_id[:8]
        state_file = self.download_dir / ".krisp_downloaded_auto.txt"
        with open(state_file, "a") as f:
            f.write(f"{short_id}\n")
        self.downloaded_ids.add(short_id)

    def _build_filename(
        self,
        original_filename: str,
        card_title: Optional[str],
        card_date: Optional[str],
    ) -> str:
        """
        Build filename: YYMMDD_EventName_krispID.ext

        1. Parse date and time from card_date / card_title
        2. Look up event in Yandex Calendar
        3. If found — use calendar event name
        4. If not — use Krisp title
        """
        ext = Path(original_filename).suffix or ".mp3"
        original_id = Path(original_filename).stem
        short_id = original_id[:8] if len(original_id) > 8 else original_id

        # Parse date: card_date → card_title → today
        parsed_date = self._parse_meeting_date(card_date, card_title)
        date_prefix = parsed_date.strftime("%y%m%d")

        # Parse time from card_title
        hour, minute = self._parse_meeting_time(card_title)

        # Look up in calendar
        event_name = None
        if hour is not None:
            try:
                from pipeline.calendar import find_event_name
                event_name = find_event_name(
                    meeting_date=parsed_date,
                    meeting_hour=hour,
                    meeting_minute=minute or 0,
                    caldav_url=os.getenv("CALDAV_URL", ""),
                    caldav_username=os.getenv("CALDAV_USERNAME", ""),
                    caldav_password=os.getenv("CALDAV_PASSWORD", ""),
                    calendar_name=os.getenv("CALDAV_CALENDAR_NAME", ""),
                )
            except Exception as e:
                logger.warning("Calendar lookup error: %s", e)

        # Determine title
        if event_name:
            title = event_name
            logger.info(f"Using calendar event name: {title}")
        else:
            title = card_title or "recording"
            logger.info(f"No calendar event, using Krisp title: {title}")

        # Sanitize title
        safe_title = "".join(
            c for c in title
            if c.isalnum() or c in " -_абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
        ).strip()
        safe_title = safe_title[:80]

        return f"{date_prefix}_{safe_title}_{short_id}{ext}"

    @staticmethod
    def _parse_meeting_date(card_date: Optional[str], card_title: Optional[str] = None) -> date:
        """Parse date from Krisp card text.

        Priority: card_date → date from card_title → today.
        card_title contains "... meeting February 10" — extract Month Day.
        """
        # 1. From card_date (if available)
        if card_date:
            for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y"):
                try:
                    return datetime.strptime(card_date.replace(",", "").strip(), fmt.replace(",", "")).date()
                except ValueError:
                    continue
            date_match = re.search(r'(\d{4})-(\d{2})-(\d{2})', card_date)
            if date_match:
                return date(int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3)))

        # 2. From card_title: "... meeting February 10" or "... meeting Feb 10"
        if card_title:
            m = re.search(r'(?:meeting|Discord)\s+(\w+)\s+(\d{1,2})', card_title, re.IGNORECASE)
            if m:
                month_name, day = m.group(1), m.group(2)
                year = date.today().year
                for fmt in ("%B %d %Y", "%b %d %Y"):
                    try:
                        return datetime.strptime(f"{month_name} {day} {year}", fmt).date()
                    except ValueError:
                        continue

        return date.today()

    @staticmethod
    def _parse_meeting_time(card_title: Optional[str]) -> tuple:
        """
        Extract time from Krisp card title.

        Formats: "0300 PM", "03:00 PM", "3:00 PM", "15:00"
        Returns: (hour_24, minute) or (None, None)
        """
        if not card_title:
            return (None, None)

        # "0300 PM" / "0300PM"
        m = re.search(r'(\d{2})(\d{2})\s*([APap][Mm])', card_title)
        if m:
            h, mi, ampm = int(m.group(1)), int(m.group(2)), m.group(3).upper()
            if ampm == "PM" and h != 12:
                h += 12
            elif ampm == "AM" and h == 12:
                h = 0
            return (h, mi)

        # "3:00 PM" / "03:00 PM"
        m = re.search(r'(\d{1,2}):(\d{2})\s*([APap][Mm])', card_title)
        if m:
            h, mi, ampm = int(m.group(1)), int(m.group(2)), m.group(3).upper()
            if ampm == "PM" and h != 12:
                h += 12
            elif ampm == "AM" and h == 12:
                h = 0
            return (h, mi)

        # "15:00" (24h)
        m = re.search(r'(\d{1,2}):(\d{2})(?!\s*[APap])', card_title)
        if m:
            return (int(m.group(1)), int(m.group(2)))

        return (None, None)

    def save_auth_state(self, context: BrowserContext):
        """Save auth state."""
        state_file = self.state_dir / "auth_state.json"
        context.storage_state(path=str(state_file))
        logger.info("✓ Auth state saved")

    def get_auth_state_path(self) -> Optional[str]:
        """Return path to auth state file (for Playwright)."""
        state_file = self.state_dir / "auth_state.json"
        if state_file.exists():
            return str(state_file)
        return None

    def _check_auth(self, page: Page) -> bool:
        """Check if auth is valid: open Meeting Notes and verify no redirect to login."""
        try:
            page.goto("https://app.krisp.ai/meeting-notes", wait_until="domcontentloaded", timeout=20000)
            time.sleep(3)
            url = page.url
            if "/login" in url or "/sign-up" in url:
                logger.warning("Auth failed: redirect to %s", url)
                return False
            # Check if recording list is present on page (sign of successful login)
            cards = page.locator("p:has-text('meeting')")
            if cards.count() > 0:
                logger.info("Auth successful, recordings on page: %d", cards.count())
                return True
            # Page loaded but no recordings found — possibly empty list or different content
            logger.info("Auth successful (Meeting Notes page loaded)")
            return True
        except Exception as e:
            logger.warning("Auth check: %s", e)
            return False

    def _run_auth_setup(self) -> None:
        """Run auth setup script."""
        script_dir = Path(__file__).parent
        auth_script = script_dir / "auth_setup.py"
        if not auth_script.exists():
            logger.error("Auth script not found: %s", auth_script)
            return
        logger.info("Auth failed. Running auth setup script...")
        logger.info("Enter OTP in the opened browser if required.")
        subprocess.run([sys.executable, str(auth_script)], cwd=str(script_dir), check=False)

    def check_and_download(self, page: Page) -> int:
        """
        Download recordings: click recording → details → ... menu → Download recording.
        """
        try:
            page.goto("https://app.krisp.ai/meeting-notes", wait_until="domcontentloaded", timeout=30000)
            # Wait for recording list to appear (various content: meeting, Discord, zoom, etc.)
            try:
                page.wait_for_selector("p:has-text('meeting')", timeout=20000)
            except Exception:
                pass
            time.sleep(3)

            # Recordings: titles in <p> elements with 'meeting' text
            cards_locator = page.locator(
                "p:has-text('meeting')"
            )
            n_cards = cards_locator.count()
            if n_cards == 0:
                logger.info("No recordings found on page")
                return 0
            logger.info("Recordings found: %d", n_cards)

            max_to_process = n_cards  # Download all recordings
            downloaded_count = 0

            for i in range(max_to_process):
                try:
                    # Each iteration: re-open list for fresh DOM (otherwise DOM goes stale)
                    page.goto("https://app.krisp.ai/meeting-notes", wait_until="domcontentloaded", timeout=30000)
                    try:
                        page.wait_for_selector("p:has-text('meeting')", timeout=15000)
                    except Exception:
                        pass
                    time.sleep(2)

                    cards_locator = page.locator(
                        "p:has-text('meeting')"
                    )
                    if cards_locator.count() <= i:
                        break
                    recording_cards = cards_locator.all()
                    card = recording_cards[i]

                    # Extract recording title from card BEFORE click
                    card_title = None
                    card_date = None
                    try:
                        card_title = card.text_content()
                        if card_title:
                            card_title = card_title.strip()
                        # Try to find date near the card
                        parent = card.locator('xpath=ancestor::div[1]')
                        if parent.count() > 0:
                            parent_text = parent.text_content()
                            if parent_text:
                                # Search for date format "Feb 7, 2026" or "7 Feb 2026" etc.
                                date_match = re.search(r'(\w{3,9}\s+\d{1,2},?\s+\d{4})', parent_text)
                                if date_match:
                                    card_date = date_match.group(1)
                    except Exception as e:
                        logger.debug(f"Failed to extract from card: {e}")

                    logger.info(f"Processing recording {i+1}/{max_to_process}: {card_title or 'unknown'}...")
                    card.click()
                    # Wait for details panel to open (header has Summarize, Share, ...)
                    try:
                        page.wait_for_selector("text=Summarize", timeout=8000)
                    except Exception:
                        pass
                    time.sleep(1)
                    # Close onboarding popup if present (blocks click on ...)
                    try:
                        page.get_by_role("button", name="Next").first.click()
                    except Exception:
                        pass
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass
                    time.sleep(0.5)

                    # "..." button in top right corner — HTML: data-tooltip-content="More actions", data-tooltip-id="action-group-tooltip"
                    menu_clicked = False
                    for sel in [
                        '[data-tooltip-content="More actions"]',
                        'button[data-tooltip-id="action-group-tooltip"]',
                        '[data-tooltip-id="action-group-tooltip"]',
                        '[aria-label="More actions"]',
                        'button[aria-label="More actions"]',
                        '[title="More actions"]',
                        '[aria-label*="More actions" i]',
                        'button[title*="More" i]',
                    ]:
                        btn = page.locator(sel).first
                        if btn.count() > 0:
                            try:
                                btn.scroll_into_view_if_needed()
                                if btn.is_visible():
                                    btn.click()
                                    time.sleep(1.5)
                                    if page.get_by_text("Download recording").count() > 0:
                                        menu_clicked = True
                                        break
                            except Exception:
                                pass
                        if menu_clicked:
                            break
                    if not menu_clicked:
                        more_btn = page.get_by_role("button", name="More actions")
                        if more_btn.count() > 0:
                            try:
                                more_btn.first.click()
                                time.sleep(1.5)
                                if page.get_by_text("Download recording").count() > 0:
                                    menu_clicked = True
                            except Exception:
                                pass
                    if not menu_clicked:
                        # JS fallback: click rightmost button in header (y < 200) — this is "..."
                        try:
                            found = page.evaluate("""() => {
                                const buttons = Array.from(document.querySelectorAll('button, [role="button"]'));
                                const inHeader = buttons.filter(b => {
                                    const r = b.getBoundingClientRect();
                                    return r.top < 200 && r.height > 0 && r.width > 0;
                                });
                                if (inHeader.length === 0) return false;
                                const byRight = inHeader.map(b => ({ el: b, right: b.getBoundingClientRect().right }));
                                byRight.sort((a, b) => b.right - a.right);
                                const toClick = byRight[1] || byRight[0];
                                toClick.el.click();
                                return true;
                            }""")
                            if found:
                                time.sleep(1.5)
                                if page.get_by_text("Download recording").count() > 0:
                                    menu_clicked = True
                        except Exception:
                            pass
                    if not menu_clicked:
                        # Click last SVG buttons on page (rightmost in header = ...)
                        try:
                            header_buttons = page.locator("button:has(svg)").all()
                            for idx in [len(header_buttons) - 1, len(header_buttons) - 2, len(header_buttons) - 3]:
                                if idx >= 0 and idx < len(header_buttons):
                                    header_buttons[idx].click()
                                    time.sleep(1.5)
                                    if page.get_by_text("Download recording").count() > 0:
                                        menu_clicked = True
                                        break
                        except Exception:
                            pass
                    if not menu_clicked:
                        logger.warning("Menu (...) not found")
                        try:
                            page.screenshot(path=str(self.download_dir / "krisp_debug_no_menu.png"))
                            logger.info("Screenshot saved: krisp_debug_no_menu.png")
                            (self.download_dir / "krisp_debug_page.html").write_text(
                                page.content(), encoding="utf-8"
                            )
                            logger.info("HTML saved: krisp_debug_page.html")
                        except Exception:
                            pass
                        page.keyboard.press("Escape")
                        time.sleep(1)
                        continue

                    # In dropdown — "Download recording"
                    download_item = page.get_by_text("Download recording", exact=False).first
                    if download_item.count() == 0:
                        page.keyboard.press("Escape")
                        time.sleep(1)
                        continue

                    # Use title from card (extracted before click)
                    meeting_title = card_title
                    meeting_date = card_date
                    logger.info(f"Using from card: title={meeting_title}, date={meeting_date}")

                    with page.expect_download(timeout=60000) as download_info:
                        download_item.click()

                    download = download_info.value
                    original_filename = download.suggested_filename or f"recording_{i+1}.mp3"

                    # Check if already downloaded (by first 8 chars of Krisp ID)
                    krisp_id_match = re.search(r'([0-9a-f]{8})', Path(original_filename).stem)
                    if krisp_id_match:
                        krisp_id_short = krisp_id_match.group(1)
                        if krisp_id_short in self.downloaded_ids:
                            logger.info(f"⏭ Skipping (already downloaded): {card_title} [{krisp_id_short}]")
                            download.cancel()
                            page.keyboard.press("Escape")
                            time.sleep(1)
                            page.keyboard.press("Escape")
                            time.sleep(1)
                            continue

                    # Build filename: YYMMDD_EventName_krispID.ext
                    filename = self._build_filename(
                        original_filename=original_filename,
                        card_title=meeting_title,
                        card_date=meeting_date,
                    )

                    filepath = self.download_dir / filename
                    download.save_as(filepath)
                    logger.info(f"✓ Downloaded: {filename}")
                    downloaded_count += 1

                    # Save Krisp ID (8 chars) to avoid re-download
                    if krisp_id_match:
                        self._save_downloaded_id(krisp_id_match.group(1))

                    page.keyboard.press("Escape")
                    time.sleep(1)
                    page.keyboard.press("Escape")
                    time.sleep(2)

                except Exception as e:
                    logger.error(f"Error processing recording {i+1}: {e}")
                    try:
                        page.keyboard.press("Escape")
                        page.keyboard.press("Escape")
                        time.sleep(1)
                    except Exception:
                        pass
                    continue

            return downloaded_count

        except Exception as e:
            logger.error(f"Error checking recordings: {e}")
            return 0

    def run_once(self, _retried_after_auth: bool = False):
        """
        Single pass: check auth → on failure run auth setup script → Meeting Notes → download recordings.
        """
        auth_state_path = self.get_auth_state_path()
        if auth_state_path:
            logger.info("Found saved auth state")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            context = browser.new_context(storage_state=auth_state_path) if auth_state_path else browser.new_context()
            context.set_default_timeout(30000)
            page = context.new_page()

            try:
                # Auth check: navigate to Meeting Notes
                if not self._check_auth(page):
                    context.close()
                    browser.close()
                    if _retried_after_auth:
                        logger.error("Retry after auth setup failed. Check login/OTP.")
                        return
                    self._run_auth_setup()
                    self.run_once(_retried_after_auth=True)
                    return

                # No saved state on first run — save after successful auth check
                if not auth_state_path:
                    self.save_auth_state(context)
                    logger.info("Auth state saved for future runs")

                # Download: Meeting Notes → select recording → three dots → Download → next
                logger.info("Checking for new recordings...")
                downloaded = self.check_and_download(page)

                if downloaded > 0:
                    logger.info("✓ Downloaded recordings: %d", downloaded)
                else:
                    logger.info("No new recordings to download")

            except Exception as e:
                logger.error("Error: %s", e, exc_info=True)
            finally:
                context.close()
                browser.close()

    def run(self):
        """Run monitoring in infinite loop."""
        logger.info("=" * 60)
        logger.info("Krisp Auto Downloader (Playwright)")
        logger.info("=" * 60)
        logger.info("Email: %s", self.email)
        logger.info("Save directory: %s", self.download_dir)
        logger.info("Check interval: %d seconds", self.check_interval)
        logger.info("Headless mode: %s", self.headless)
        logger.info("=" * 60)
        logger.info("")

        while True:
            try:
                self.run_once()

                logger.info("")
                logger.info(f"Waiting {self.check_interval} seconds until next check...")
                logger.info("")
                time.sleep(self.check_interval)

            except KeyboardInterrupt:
                logger.info("Stop signal received")
                break
            except Exception as e:
                logger.error("Critical error: %s", e, exc_info=True)
                logger.info("Waiting 60 seconds before retry...")
                time.sleep(60)


def main():
    """Main function."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Krisp Auto Downloader — fully automated via Playwright"
    )
    parser.add_argument(
        "--email",
        required=True,
        help="Krisp login email",
    )
    parser.add_argument(
        "--download-dir",
        required=True,
        help="Directory for saving recordings",
    )
    parser.add_argument(
        "--check-interval",
        type=int,
        default=300,
        help="Check interval for new recordings (seconds)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=True,
        help="Run in headless mode (no GUI)",
    )
    parser.add_argument(
        "--visible",
        action="store_true",
        help="Run with visible browser (for debugging)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Single pass (download and exit)",
    )

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler("krisp_auto_downloader.log"),
            logging.StreamHandler(),
        ],
    )

    downloader = KrispAutoDownloader(
        download_dir=Path(args.download_dir),
        email=args.email,
        check_interval=args.check_interval,
        headless=not args.visible,
    )

    if args.once:
        downloader.run_once()
    else:
        downloader.run()


if __name__ == "__main__":
    main()
