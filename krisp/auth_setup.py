#!/usr/bin/env python3
"""
Automatic auth setup — opens a browser for manual login.
"""

from playwright.sync_api import sync_playwright
from pathlib import Path
import time

def setup_auth():
    """Open browser for Krisp authorization (2-minute window)."""

    print("=" * 60)
    print("KRISP AUTH SETUP")
    print("=" * 60)
    print()
    print("A browser window will open for you to log in.")
    print()
    print("STEPS:")
    print("  1. Log in with your Krisp email + OTP code")
    print("  2. Navigate to https://app.krisp.ai/meeting-notes")
    print("  3. Verify you can see the recordings list")
    print("  4. Wait — auth state will be saved automatically")
    print()
    print("Opening browser in 3 seconds...")
    time.sleep(3)
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=['--start-maximized']
        )

        context = browser.new_context(
            viewport=None,
            no_viewport=True
        )

        page = context.new_page()

        print("Browser opened")
        print("Loading login page...")
        page.goto('https://app.krisp.ai/login', wait_until='domcontentloaded')

        print()
        print("-" * 60)
        print("You have 2 MINUTES to log in...")
        print("-" * 60)
        print()

        for i in range(120, 0, -15):
            print(f"  Remaining: {i} seconds...")
            time.sleep(15)

        print()
        print("Time's up!")
        print()

        current_url = page.url
        print(f"  Current URL: {current_url}")
        print()

        # Save cookies and localStorage via Playwright API
        state_dir = Path('.krisp_state')
        state_dir.mkdir(exist_ok=True)
        state_file = state_dir / 'auth_state.json'

        print("  Saving cookies and localStorage...")
        context.storage_state(path=str(state_file))
        print(f"  Saved: {state_file.absolute()}")
        print()
        print("  This file will be used for automatic login in future runs.")
        print()
        print()

        if '/meeting' in current_url:
            print("=" * 60)
            print("SUCCESS! You are on the recordings page.")
            print("=" * 60)
        elif '/login' in current_url or '/sign-up' in current_url:
            print("=" * 60)
            print("WARNING: You are still on the login page.")
            print("=" * 60)
            print()
            print("You may not have had enough time to log in.")
            print("Run the script again: python3 auth_setup.py")
        else:
            print("=" * 60)
            print("Auth state saved")
            print("=" * 60)

        print()
        print("Closing browser...")
        browser.close()

        print()
        print("Done!")
        print()
        print("Next step: run the downloader (e.g., python krisp/downloader.py)")
        print()

if __name__ == "__main__":
    setup_auth()
