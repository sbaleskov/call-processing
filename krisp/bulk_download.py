#!/usr/bin/env python3
"""
Download undownloaded Crisp calls with pagination support.

Usage:
    python bulk_download.py --dry-run                    # List what needs downloading
    python bulk_download.py --test 1                     # Download 1 file to verify
    python bulk_download.py --cutoff-date 2026-01-18     # Download all up to Jan 18
    python bulk_download.py                              # Download all undownloaded
"""

import os
import re
import sys
import time
import argparse
import logging
from pathlib import Path
from datetime import datetime, date
from typing import List, Dict, Optional, Set

from playwright.sync_api import sync_playwright, Page
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

AUTH_STATE = Path(__file__).parent / ".krisp_state" / "auth_state.json"
DOWNLOAD_DIR = Path(os.getenv("WATCH_DIR", str(Path(__file__).parent.parent / "meetings")))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent.parent / "download_all_calls.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ─────────────────── State Management ─────────────────────

def load_downloaded_ids() -> Set[str]:
    ids = set()
    state_file = DOWNLOAD_DIR / ".krisp_downloaded_auto.txt"
    if state_file.exists():
        for line in state_file.read_text().strip().split("\n"):
            if line.strip():
                ids.add(line.strip()[:8])
    for f in DOWNLOAD_DIR.iterdir():
        if f.suffix.lower() in ('.mp3', '.mp4', '.m4a', '.wav'):
            match = re.search(r'_([0-9a-f]{8})[0-9a-f]*\.\w+$', f.name)
            if match:
                ids.add(match.group(1))
    return ids


def save_downloaded_id(recording_id: str):
    short_id = recording_id[:8]
    state_file = DOWNLOAD_DIR / ".krisp_downloaded_auto.txt"
    with open(state_file, "a") as f:
        f.write(f"{short_id}\n")


def parse_meeting_date(date_text: str) -> Optional[date]:
    """Parse date from column text like 'TagSFeb 20', 'TagSJan 18', 'Tag19/12/25'."""
    if not date_text:
        return None

    # Search for "Month Day" pattern directly (ignore prefix garbage)
    m = re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})', date_text, re.IGNORECASE)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)} 2026", "%b %d %Y").date()
        except ValueError:
            pass

    # Search for "DD/MM/YY" pattern
    m = re.search(r'(\d{1,2})/(\d{1,2})/(\d{2})', date_text)
    if m:
        try:
            return date(2000 + int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass

    return None


def dismiss_modals(page: Page):
    try:
        page.evaluate("""() => {
            document.querySelectorAll('.ReactModal__Overlay').forEach(o => o.remove());
            document.querySelectorAll('.ReactModalPortal').forEach(p => {
                if (p.children.length > 0) p.innerHTML = '';
            });
        }""")
    except Exception:
        pass
    try:
        page.keyboard.press("Escape")
        time.sleep(0.3)
    except Exception:
        pass


# ─────────────────── Page Operations ─────────────────────

def wait_for_meeting_list(page: Page, timeout: int = 15):
    """Wait until meeting list is loaded (checkboxes appear)."""
    for _ in range(timeout):
        count = page.locator('input[type="checkbox"][id^="check"]').count()
        if count > 0:
            return True
        time.sleep(1)
    return False


def go_to_meetings_page(page: Page, target_page: int = 1):
    """Navigate to meetings page and optionally to a specific page number."""
    page.goto("https://app.krisp.ai/meeting-notes", wait_until="domcontentloaded", timeout=30000)
    time.sleep(5)
    dismiss_modals(page)
    wait_for_meeting_list(page)

    if target_page > 1:
        for i in range(target_page - 1):
            if not click_next_page(page):
                logger.warning(f"Could not reach page {target_page} (stopped at {i+1})")
                return False
    return True


def get_page_meetings(page: Page) -> List[Dict]:
    """Extract all meeting info from the current page."""
    # Wait for list to stabilize
    time.sleep(1)
    return page.evaluate("""() => {
        const rows = document.querySelectorAll('div.row.align-items-center');
        const meetings = [];
        for (const row of rows) {
            if (row.classList.contains('justify-content-between')) continue;
            if (row.classList.contains('py-3')) continue;

            const checkbox = row.querySelector('input[type="checkbox"][id^="check"]');
            if (!checkbox) continue;
            const krispId = checkbox.id.replace('check', '');

            let title = '';
            const pElements = row.querySelectorAll('p');
            for (const p of pElements) {
                const text = p.textContent.trim();
                if (text.length > 3 && text.length < 150) {
                    title = text;
                    break;
                }
            }

            let dateText = '';
            const cols = row.children;
            if (cols.length >= 2) {
                dateText = cols[cols.length - 1].textContent.trim();
            }

            meetings.push({
                krispId: krispId,
                title: title || row.textContent.trim().substring(0, 80),
                dateText: dateText
            });
        }
        return meetings;
    }""")


def get_pagination_info(page: Page) -> Optional[str]:
    try:
        el = page.locator("text=/\\d+\\s*-\\s*\\d+\\s*of\\s*\\d+/")
        if el.count() > 0:
            return el.first.text_content().strip()
    except Exception:
        pass
    return None


def get_first_krisp_id(page: Page) -> Optional[str]:
    """Get the first checkbox ID on the page to verify page content changed."""
    try:
        cb = page.locator('input[type="checkbox"][id^="check"]').first
        if cb.count() > 0:
            return cb.get_attribute("id")
    except Exception:
        pass
    return None


def click_next_page(page: Page) -> bool:
    """Click next page button. Verifies content actually changed."""
    try:
        dismiss_modals(page)
        time.sleep(0.5)

        old_first_id = get_first_krisp_id(page)

        icon_buttons = page.locator('button[data-test-id="Pagination"].btn-v2-icon')
        if icon_buttons.count() < 2:
            return False

        next_btn = icon_buttons.nth(1)
        if next_btn.is_disabled():
            return False

        next_btn.click(force=True)

        # Wait for content to change (verify first checkbox ID changed)
        for _ in range(15):
            time.sleep(1)
            new_first_id = get_first_krisp_id(page)
            if new_first_id and new_first_id != old_first_id:
                time.sleep(1)  # Extra settle time
                return True

        # Pagination might have worked even if first ID didn't change (unlikely)
        time.sleep(3)
        return True

    except Exception as e:
        logger.error(f"Error clicking next page: {e}")
        return False


# ─────────────────── Download Logic ─────────────────────

def download_one_recording(page: Page, krisp_id: str, title: str, page_num: int) -> Optional[Path]:
    """
    Download a single recording. Re-navigates to the correct page each time for reliability.
    """
    try:
        # Navigate fresh to the meetings page and to the correct page
        go_to_meetings_page(page, page_num)

        # Find the row by checkbox ID
        checkbox_label = page.locator(f'label[for="check{krisp_id}"]')
        if checkbox_label.count() == 0:
            logger.warning(f"  Row not found for [{krisp_id[:8]}] on page {page_num}")
            return None

        # Click on a <p> element in the row to open details
        row = checkbox_label.locator('xpath=ancestor::div[contains(@class, "row")][contains(@class, "align-items-center")]')
        if row.count() == 0:
            logger.warning(f"  Row container not found for [{krisp_id[:8]}]")
            return None

        clicked = False
        p_elements = row.locator('p')
        for i in range(p_elements.count()):
            el = p_elements.nth(i)
            text = (el.text_content() or "").strip()
            if len(text) > 3:
                el.click()
                clicked = True
                break

        if not clicked:
            row.locator('[data-test-id="ListItem"]').first.click()

        time.sleep(3)

        # Wait for details panel
        try:
            page.wait_for_selector("text=Summarize", timeout=10000)
        except Exception:
            pass
        time.sleep(1)

        # Dismiss onboarding/popups
        dismiss_modals(page)
        time.sleep(0.5)

        # Find "..." (More actions) menu
        menu_clicked = False
        for sel in [
            '[data-tooltip-content="More actions"]',
            'button[data-tooltip-id="action-group-tooltip"]',
            '[aria-label="More actions"]',
            'button[aria-label="More actions"]',
        ]:
            btn = page.locator(sel).first
            if btn.count() > 0:
                try:
                    if btn.is_visible():
                        btn.click()
                        time.sleep(2)
                        if page.get_by_text("Download recording").count() > 0:
                            menu_clicked = True
                            break
                except Exception:
                    pass

        if not menu_clicked:
            # JS fallback: find the More actions button
            try:
                found = page.evaluate("""() => {
                    const buttons = Array.from(document.querySelectorAll('button, [role="button"]'));
                    const inHeader = buttons.filter(b => {
                        const r = b.getBoundingClientRect();
                        return r.top < 200 && r.height > 0 && r.width > 0 && r.width < 60;
                    });
                    if (inHeader.length === 0) return false;
                    const byRight = inHeader.sort((a, b) =>
                        b.getBoundingClientRect().right - a.getBoundingClientRect().right
                    );
                    for (let i = 0; i < Math.min(4, byRight.length); i++) {
                        byRight[i].click();
                        return true;
                    }
                    return false;
                }""")
                if found:
                    time.sleep(2)
                    if page.get_by_text("Download recording").count() > 0:
                        menu_clicked = True
            except Exception:
                pass

        if not menu_clicked:
            logger.warning(f"  'More actions' menu not found — skipping [{krisp_id[:8]}]")
            # Mark as downloaded to avoid retrying
            save_downloaded_id(krisp_id)
            return None

        # Click "Download recording"
        download_item = page.get_by_text("Download recording", exact=False).first
        if download_item.count() == 0:
            logger.warning(f"  'Download recording' option missing for [{krisp_id[:8]}]")
            save_downloaded_id(krisp_id)
            page.keyboard.press("Escape")
            time.sleep(1)
            return None

        with page.expect_download(timeout=300000) as download_info:
            download_item.click()

        download = download_info.value
        original_filename = download.suggested_filename or f"{krisp_id}.mp3"

        filepath = DOWNLOAD_DIR / original_filename
        download.save_as(filepath)

        size_kb = filepath.stat().st_size / 1024
        logger.info(f"  -> Saved: {original_filename} ({size_kb:.0f} KB)")

        save_downloaded_id(krisp_id)
        return filepath

    except Exception as e:
        logger.error(f"  Error downloading [{krisp_id[:8]}]: {e}")
        try:
            page.keyboard.press("Escape")
            time.sleep(0.5)
        except Exception:
            pass
        return None


# ─────────────────── Main ─────────────────────

def main():
    parser = argparse.ArgumentParser(description="Download undownloaded Crisp calls")
    parser.add_argument("--visible", action="store_true", help="Show browser window")
    parser.add_argument("--max-pages", type=int, default=0, help="Max pages (0=all)")
    parser.add_argument("--dry-run", action="store_true", help="List without downloading")
    parser.add_argument("--test", type=int, default=0, help="Download only N files")
    parser.add_argument("--cutoff-date", type=str, default="",
                        help="Only meetings on/before this date (YYYY-MM-DD)")
    parser.add_argument("--start-page", type=int, default=1, help="Start page number")
    args = parser.parse_args()

    cutoff = None
    if args.cutoff_date:
        cutoff = datetime.strptime(args.cutoff_date, "%Y-%m-%d").date()
        logger.info(f"Cutoff date: {cutoff}")

    if not AUTH_STATE.exists():
        logger.error(f"Auth state not found: {AUTH_STATE}")
        sys.exit(1)

    downloaded_ids = load_downloaded_ids()
    logger.info(f"Already downloaded: {len(downloaded_ids)} recordings")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.visible)
        context = browser.new_context(storage_state=str(AUTH_STATE))
        context.set_default_timeout(30000)
        page = context.new_page()

        try:
            # Auth check
            page.goto("https://app.krisp.ai/meeting-notes", wait_until="domcontentloaded", timeout=30000)
            time.sleep(5)
            if "/login" in page.url or "/sign-up" in page.url:
                logger.error("Auth failed")
                return

            dismiss_modals(page)
            wait_for_meeting_list(page)

            pag_text = get_pagination_info(page)
            total_count = 0
            if pag_text:
                m = re.search(r'of\s+(\d+)', pag_text)
                if m:
                    total_count = int(m.group(1))
            logger.info(f"Total meetings: {total_count}")

            # Phase 1: Scan all pages and collect items to download
            logger.info("\n=== Phase 1: Scanning pages ===")
            all_to_download = []  # (page_num, krisp_id, title, date_text)
            page_num = args.start_page

            # Navigate to start page
            if args.start_page > 1:
                go_to_meetings_page(page, args.start_page)

            total_skipped = 0
            total_filtered = 0

            while True:
                pag_info = get_pagination_info(page)
                meetings = get_page_meetings(page)
                logger.info(f"\nPage {page_num} [{pag_info}]: {len(meetings)} meetings")

                page_new = 0
                for m in meetings:
                    short_id = m['krispId'][:8]

                    if short_id in downloaded_ids:
                        total_skipped += 1
                        continue

                    if cutoff:
                        meeting_date = parse_meeting_date(m['dateText'])
                        if meeting_date and meeting_date > cutoff:
                            total_filtered += 1
                            continue

                    all_to_download.append((page_num, m['krispId'], m['title'], m['dateText']))
                    page_new += 1

                logger.info(f"  New: {page_new}, Skipped: {total_skipped}, Filtered: {total_filtered}")

                if args.max_pages > 0 and page_num >= (args.start_page + args.max_pages - 1):
                    break

                if not click_next_page(page):
                    break

                page_num += 1

            logger.info(f"\n=== Scan complete ===")
            logger.info(f"Total to download: {len(all_to_download)}")
            logger.info(f"Already had: {total_skipped}")
            logger.info(f"Filtered by date: {total_filtered}")

            if args.dry_run:
                logger.info("\n=== Dry-run: items to download ===")
                for pg, kid, title, dt in all_to_download:
                    parsed = parse_meeting_date(dt)
                    logger.info(f"  p{pg} [{kid[:8]}] {parsed or dt:>12} | {title[:60]}")
                return

            # Phase 2: Download
            logger.info(f"\n=== Phase 2: Downloading {len(all_to_download)} recordings ===")
            total_downloaded = 0
            total_errors = 0

            for i, (pg, kid, title, dt) in enumerate(all_to_download):
                if args.test > 0 and total_downloaded >= args.test:
                    logger.info(f"Test limit reached ({args.test})")
                    break

                logger.info(f"\n[{i+1}/{len(all_to_download)}] {title[:50]} [{kid[:8]}] (page {pg})")

                result = download_one_recording(page, kid, title, pg)
                if result:
                    total_downloaded += 1
                    downloaded_ids.add(kid[:8])
                else:
                    total_errors += 1

            logger.info(f"\n{'='*60}")
            logger.info(f"DONE: Downloaded {total_downloaded}, Errors {total_errors}")
            logger.info(f"{'='*60}")

        except Exception as e:
            logger.error(f"Fatal: {e}", exc_info=True)
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
