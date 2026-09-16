import argparse
import os
import sys
import webbrowser
import uvicorn
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import config

def main():
    parser = argparse.ArgumentParser(description="Motive Safety Camera Console Launcher")
    parser.add_argument("--port", type=int, default=config.PORT, help="Port to run web console on")
    parser.add_argument("--host", type=str, default=config.HOST, help="Host address to bind to")
    parser.add_argument("--setup", action="store_true", help="Launch Motive browser immediately for initial login / 2FA")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless background mode")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open console in system browser")
    
    args = parser.parse_args()

    print("=" * 65)
    print("   ROADSIDE FLEET SAFETY MONITORING CONSOLE")
    print("   Target Fleet: HEART & Unassigned Groups | In Transit & Idling")
    print("=" * 65)
    print(f"[*] Console Dashboard: http://{args.host}:{args.port}")
    print(f"[*] Motive Profile Dir: {config.BROWSER_PROFILE_DIR}")
    print("=" * 65)

    if not args.no_browser:
        # Schedule browser launch
        webbrowser.open(f"http://{args.host}:{args.port}")

    # Launch FastAPI / Uvicorn
    uvicorn.run("server:app", host=args.host, port=args.port, reload=False)

if __name__ == "__main__":
    main()
