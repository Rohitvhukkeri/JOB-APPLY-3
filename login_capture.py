"""
Run this once per site (Naukri, LinkedIn) before running the apply scripts.

It opens a real, visible Chromium window. YOU log in by hand — type your
password, complete 2FA, solve any CAPTCHA yourself. Once you're on the
logged-in homepage, come back to this terminal and press Enter. The script
then saves your session (cookies + local storage) to a local file so the
apply scripts can reuse it without ever seeing your password.

Usage:
    python login_capture.py naukri
    python login_capture.py linkedin
    python login_capture.py naukri --wait   # saves automatically once you leave the login page
"""
import sys
from playwright.sync_api import sync_playwright

SITES = {
    "naukri": "https://www.naukri.com/nlogin/login",
    "linkedin": "https://www.linkedin.com/login",
}


def main():
    wait_mode = "--wait" in sys.argv[1:]
    args = [a for a in sys.argv[1:] if a != "--wait"]
    if len(args) != 1 or args[0] not in SITES:
        print(f"Usage: python login_capture.py [{'|'.join(SITES)}] [--wait]")
        sys.exit(1)

    site = args[0]
    url = SITES[site]
    out_path = f"session_{site}.json"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(url)

        print(f"\nA browser window is open at {url}")
        print("Log in by hand: password, 2FA, any CAPTCHA — all of it.")
        if not wait_mode:
            input("Once you're on the logged-in homepage, press Enter here to save the session... ")
        else:
            # No terminal to press Enter in -- wait until the page leaves the
            # login screen instead (up to 10 minutes).
            print("Waiting for you to finish logging in (up to 10 minutes)...")
            page.wait_for_url(lambda u: "login" not in u.lower(), timeout=600_000)
            page.wait_for_timeout(5000)  # let post-login cookies settle

        context.storage_state(path=out_path)
        print(f"Session saved to {out_path}. Keep this file private — it's equivalent to being logged in.")
        browser.close()


if __name__ == "__main__":
    main()
