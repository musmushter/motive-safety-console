import argparse
import os
import subprocess
import sys
import time
import webbrowser
import httpx
import uvicorn
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import config

def is_chrome_cdp_running() -> bool:
    try:
        r = httpx.get("http://127.0.0.1:9222/json/version", timeout=1.5)
        return r.status_code == 200
    except Exception:
        return False

def ensure_chrome_profiles_running():
    """
    Ensures Chrome is launched with port 9222 and opens both profiles:
    1. Default (HEART)
    2. Profile 1 (Houston Towing)
    """
    chrome_exe = config.CHROME_PATH
    user_data_dir = config.CHROME_USER_DATA_DIR

    if not chrome_exe.exists():
        local_chrome = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe"
        if local_chrome.exists():
            chrome_exe = local_chrome
        else:
            print("[!] Warning: Could not locate chrome.exe. Please launch Chrome manually with debugging port 9222.")
            return

    cdp_alive = is_chrome_cdp_running()
    if not cdp_alive:
        print("[*] Launching Chrome with remote debugging on port 9222...")
        # Launch Primary Profile (Default - HEART)
        cmd_profile1 = [
            str(chrome_exe),
            "--remote-debugging-port=9222",
            f"--user-data-dir={user_data_dir}",
            "--profile-directory=Default",
            config.MOTIVE_URL
        ]
        subprocess.Popen(cmd_profile1)
        print("[*] Opened HEART profile (Default).")

        # Wait for CDP port to come up
        for _ in range(15):
            time.sleep(0.5)
            if is_chrome_cdp_running():
                break

        # Launch Secondary Profile (Profile 1 - Houston Towing)
        cmd_profile2 = [
            str(chrome_exe),
            f"--user-data-dir={user_data_dir}",
            "--profile-directory=Profile 1",
            config.MOTIVE_URL
        ]
        subprocess.Popen(cmd_profile2)
        print("[*] Opened Houston Towing profile (Profile 1).")
    else:
        print("[+] Chrome is already running with remote debugging on port 9222.")
        try:
            r = httpx.get("http://127.0.0.1:9222/json", timeout=2.0)
            pages = r.json()
            motive_pages = [p for p in pages if "gomotive.com" in p.get("url", "")]
            if len(motive_pages) < 2:
                print("[*] Ensuring Houston Towing profile window is open...")
                subprocess.Popen([
                    str(chrome_exe),
                    f"--user-data-dir={user_data_dir}",
                    "--profile-directory=Profile 1",
                    config.MOTIVE_URL
                ])
        except Exception:
            pass

def main():
    parser = argparse.ArgumentParser(description="Motive Safety Camera Console Launcher")
    parser.add_argument("--port", type=int, default=config.PORT, help="Port to run web console on")
    parser.add_argument("--host", type=str, default=config.HOST, help="Host address to bind to")
    parser.add_argument("--setup", action="store_true", help="Launch Motive browser immediately for initial login / 2FA")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless background mode")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open console in system browser")
    parser.add_argument("--no-chrome-launch", action="store_true", help="Don't auto-launch Chrome profiles")
    
    args = parser.parse_args()

    print("=" * 65)
    print("   ROADSIDE FLEET SAFETY MONITORING CONSOLE")
    print("   Organizations: HEART (260129) & Houston Towing (599530)")
    print("=" * 65)
    print(f"[*] Console Dashboard: http://{args.host}:{args.port}")
    print(f"[*] Motive User Data Dir: {config.CHROME_USER_DATA_DIR}")
    print("=" * 65)

    if not args.no_chrome_launch:
        ensure_chrome_profiles_running()

    if not args.no_browser:
        # Schedule browser launch
        webbrowser.open(f"http://{args.host}:{args.port}")

    # Launch FastAPI / Uvicorn
    uvicorn.run("server:app", host=args.host, port=args.port, reload=False)

if __name__ == "__main__":
    main()

