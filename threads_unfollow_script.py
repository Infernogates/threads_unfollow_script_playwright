#!/usr/bin/env python3
"""
USAGE:

1. USERNAME alanını kendi Threads kullanıcı adınızla değiştirin (başında @ olmadan).
2. Bu dosyayı bir terminalde çalıştırın: python threads_unfollow_script.py
3. En iyi sonuç için bilgisayarda Chrome tarayıcısının açık olduğundan ve Threads hesabınıza giriş yaptığınızdan emin olun.
4. DRY_RUN = True olarak ayarlanmışsa, yalnızca rapor verir;
    DRY_RUN = False olarak ayarlanmışsa, gerçekten takipten çıkarır.
"""

import os
import re
import sys
import time
import socket
import random
import shutil
import platform
import subprocess
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ----------------------------- CONFIG ---------------------------------------

USERNAME = "THREADS_KULLANICI_ADI"      # your Threads username, WITHOUT the leading @

DRY_RUN = False                     # True = only report; False = actually unfollow
MAX_UNFOLLOWS_PER_RUN = 250         # safety cap per run -- keep this small
MIN_DELAY_SECONDS = 8              # min pause between unfollows
MAX_DELAY_SECONDS = 20             # max pause between unfollows

# Accounts you never want to unfollow, even if they don't follow you back.
WHITELIST = {
    # "favourite_account",
}

# A dedicated Chrome profile for this (kept separate from your everyday Chrome).
# You log into Threads here once; the session persists across runs.
CHROME_PROFILE_DIR = Path.home() / "threads-automation-profile"
DEBUG_PORT = 9222                  # DevTools port the script attaches to

BASE_URL = "https://www.threads.com"

# How hard to try scrolling the follower/following lists.
SCROLL_PAUSE_SECONDS = 1.2
SCROLL_STABLE_ROUNDS = 4           # stop after this many scrolls with no new names
SCROLL_MAX_ROUNDS = 400            # hard cap so it can't loop forever

# ---------------------------------------------------------------------------


def human_pause(lo, hi):
    time.sleep(random.uniform(lo, hi))


def extract_handle(href):
    """Pull 'name' out of an href like '/@name' or 'https://.../@name/post/..'."""
    if not href:
        return None
    m = re.search(r"/@([A-Za-z0-9._]+)", href)
    return m.group(1).lower() if m else None


def has_session_cookie(ctx):
    """True once Threads/Instagram has set an auth session cookie."""
    for c in ctx.cookies():
        if c.get("name") == "sessionid" and c.get("value"):
            return True
    return False


def find_chrome():
    """Locate the real Google Chrome binary for the current OS, or None."""
    system = platform.system()
    candidates = []
    if system == "Darwin":
        candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    elif system == "Windows":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    else:  # Linux / other
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            found = shutil.which(name)
            if found:
                candidates.append(found)
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def port_is_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def manual_launch_hint():
    """The command to launch Chrome yourself if auto-launch can't find it."""
    profile = str(CHROME_PROFILE_DIR)
    system = platform.system()
    if system == "Darwin":
        chrome = '"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"'
    elif system == "Windows":
        chrome = r'"C:\Program Files\Google\Chrome\Application\chrome.exe"'
    else:
        chrome = "google-chrome"
    return (f'{chrome} --remote-debugging-port={DEBUG_PORT} '
            f'--user-data-dir="{profile}"')


def ensure_chrome_running():
    """
    Make sure a debuggable Chrome is listening on DEBUG_PORT. If one is already
    running we reuse it; otherwise we launch your real Chrome with a dedicated
    profile and the debugging port enabled. (A custom --user-data-dir is
    required: recent Chrome refuses remote debugging on the default profile.)
    """
    if port_is_open(DEBUG_PORT):
        print(f"Found a Chrome already listening on port {DEBUG_PORT} -- reusing it.")
        return

    chrome = find_chrome()
    if not chrome:
        sys.exit(
            "Couldn't find Google Chrome automatically.\n"
            "Launch it yourself in a terminal with:\n\n  "
            + manual_launch_hint()
            + "\n\nthen log into Threads in that window and run this script again."
        )

    CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print("Launching your real Chrome with a dedicated profile...")
    subprocess.Popen([
        chrome,
        f"--remote-debugging-port={DEBUG_PORT}",
        f"--user-data-dir={CHROME_PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        BASE_URL + "/login",
    ])
    for _ in range(60):                 # wait up to ~30s for the port
        if port_is_open(DEBUG_PORT):
            return
        time.sleep(0.5)
    sys.exit("Chrome didn't expose its debugging port in time. Try launching it "
             "manually:\n\n  " + manual_launch_hint())


def collect_handles_from_open_dialog(page):
    handles = set()
    dialog = page.locator('div[role="dialog"]').last
    dialog.wait_for(state="visible", timeout=15000)

    stable = 0
    for _ in range(SCROLL_MAX_ROUNDS):
        hrefs = dialog.locator('a[href*="/@"]').evaluate_all(
            "els => els.map(e => e.getAttribute('href'))"
        )
        before = len(handles)
        for h in hrefs:
            handle = extract_handle(h)
            if handle:
                handles.add(handle)

        page.evaluate(
            """(dlg) => {
                let best = dlg, max = 0;
                dlg.querySelectorAll('*').forEach(el => {
                    const d = el.scrollHeight - el.clientHeight;
                    if (d > max && el.clientHeight > 0) { max = d; best = el; }
                });
                best.scrollTop = best.scrollHeight;
            }""",
            dialog.element_handle(),
        )
        time.sleep(SCROLL_PAUSE_SECONDS)

        if len(handles) == before:
            stable += 1
            if stable >= SCROLL_STABLE_ROUNDS:
                break
        else:
            stable = 0

    return handles


def open_list_dialog(page, which):
    """
    which: 'Followers' or 'Following'. Opens the dialog on your profile page and
    returns the set of handles in that tab.
    """
    page.goto(f"{BASE_URL}/@{USERNAME}", wait_until="domcontentloaded")
    human_pause(2, 4)

    # Click the "X followers" entry point to open the dialog.
    opener = page.get_by_text(re.compile(r"\bfollowers\b", re.I)).first
    opener.click()
    human_pause(1.5, 3)

    dialog = page.locator('div[role="dialog"]').last
    dialog.wait_for(state="visible", timeout=15000)

    # Select the right tab inside the dialog.
    tab = dialog.get_by_role("tab", name=re.compile(rf"^{which}", re.I))
    if tab.count() == 0:
        tab = dialog.get_by_text(re.compile(rf"^{which}\b", re.I)).first
    try:
        tab.click()
        human_pause(1, 2)
    except PWTimeout:
        pass

    handles = collect_handles_from_open_dialog(page)

    page.keyboard.press("Escape")
    human_pause(1, 2)
    return handles


def unfollow(page, handle):
    page.goto(f"{BASE_URL}/@{handle}", wait_until="domcontentloaded")
    human_pause(2, 4)

    following_btn = page.get_by_role(
        "button", name=re.compile(r"^Following$", re.I)
    ).first
    if following_btn.count() == 0:
        print(f"  - @{handle}: no 'Following' button found (already unfollowed?)")
        return False

    try:
        following_btn.click()
    except PWTimeout:
        print(f"  - @{handle}: couldn't click 'Following'")
        return False
    human_pause(0.8, 1.6)

    confirm = page.get_by_role("button", name=re.compile(r"^Unfollow$", re.I)).first
    if confirm.count() == 0:
        confirm = page.get_by_text(re.compile(r"^Unfollow$", re.I)).first
    try:
        confirm.click(timeout=5000)
    except PWTimeout:
        print(f"  - @{handle}: confirmation not found")
        return False

    human_pause(1, 2)
    return True


def main():
    if USERNAME == "your_handle_here" or not USERNAME:
        sys.exit("Set USERNAME at the top of the script to your Threads handle first.")

    me = USERNAME.lower()

    # Start (or reuse) a normal Chrome with remote debugging enabled.
    ensure_chrome_running()

    with sync_playwright() as p:
        # Attach to that Chrome rather than launching our own automated browser.
        # Because the session is created in a normal browser, Meta won't bounce
        # it back to login the way it does with a Playwright-launched browser.
        browser = p.chromium.connect_over_cdp(f"http://localhost:{DEBUG_PORT}")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()

        # Wait until you're logged in (detected via the session cookie).
        if not has_session_cookie(context):
            try:
                page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded")
            except PWTimeout:
                pass
            print("\nLog in to Threads in the Chrome window that opened.")
            print("Complete any 2FA, and tap 'Save login info' / 'Trust' if asked.")
            print("Waiting for login (up to 5 minutes)...")
            deadline = time.time() + 300
            while time.time() < deadline:
                if has_session_cookie(context):
                    break
                time.sleep(2)
            else:
                sys.exit("Didn't detect a logged-in session. See the notes below.")
        print("Login detected.")
        human_pause(1, 2)

        print("Reading your Following list...")
        following = open_list_dialog(page, "Following")
        print(f"  found {len(following)} accounts you follow")

        print("Reading your Followers list...")
        followers = open_list_dialog(page, "Followers")
        print(f"  found {len(followers)} accounts that follow you")

        if not following or not followers:
            print(
                "\nGot 0 for one of the lists -- the page structure probably "
                "changed and the selectors need updating. The most reliable "
                "fallback is Meta's data export (Settings > Download your "
                "information > Threads, JSON), which gives exact followers.json "
                "and following.json files."
            )

        whitelist = {w.lower() for w in WHITELIST}
        non_followers = sorted((following - followers - whitelist) - {me})

        print(f"\n{len(non_followers)} accounts don't follow you back:")
        for h in non_followers:
            print(f"  @{h}")

        if not non_followers:
            print("Everyone you follow follows you back. Nothing to do.")
            return

        if DRY_RUN:
            print("\nDRY_RUN is on -- nothing was unfollowed.")
            print("Set DRY_RUN = False near the top and run again to unfollow.")
            return

        targets = non_followers[:MAX_UNFOLLOWS_PER_RUN]
        print(
            f"\nUnfollowing up to {len(targets)} this run "
            f"(cap = {MAX_UNFOLLOWS_PER_RUN})..."
        )

        done = 0
        for h in targets:
            if unfollow(page, h):
                done += 1
                print(f"  ok  unfollowed @{h}  ({done}/{len(targets)})")
            human_pause(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)

        print(f"\nDone. Unfollowed {done} this run.")
        if len(non_followers) > len(targets):
            print(
                f"{len(non_followers) - len(targets)} still to go -- "
                f"run again later to continue."
            )
        print("(Your Chrome stays open -- close it yourself when done.)")


if __name__ == "__main__":
    main()
