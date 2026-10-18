import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SNAPSHOTS_DIR = BASE_DIR / "snapshots"
STATIC_DIR = BASE_DIR / "static"
BROWSER_PROFILE_DIR = DATA_DIR / "browser_profile"

# Ensure runtime directories exist
DATA_DIR.mkdir(exist_ok=True)
SNAPSHOTS_DIR.mkdir(exist_ok=True)
BROWSER_PROFILE_DIR.mkdir(exist_ok=True)

# Filtering criteria based on user requirements:
# 1. Target groups: "HEART" and unassigned/no group
TARGET_GROUPS = ["HEART", "", None]

# 2. Target vehicle states: In transit and idling
TARGET_STATUSES = ["in transit", "transit", "moving", "idling", "idle"]

# Motive URL
MOTIVE_URL = "https://app.gomotive.com/en-US"

# Web server config
PORT = 8000
HOST = "127.0.0.1"

# Chrome configuration for multi-organization monitoring
import platform
import shutil

if platform.system() == "Windows":
    _win_chrome_candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    _detected_win_chrome = next((p for p in _win_chrome_candidates if p.exists()), _win_chrome_candidates[0])
    CHROME_PATH = Path(os.environ.get("CHROME_PATH", _detected_win_chrome))
    _default_win_profile = Path.home() / "fsl_tracker" / "console_profile"
    if not _default_win_profile.exists():
        _default_win_profile = BROWSER_PROFILE_DIR
    CHROME_USER_DATA_DIR = Path(os.environ.get("CHROME_USER_DATA_DIR", _default_win_profile))
else:
    # Linux / macOS
    _detected_chrome = (
        shutil.which("google-chrome")
        or shutil.which("google-chrome-stable")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("chrome")
        or "/usr/bin/chromium"
    )
    CHROME_PATH = Path(os.environ.get("CHROME_PATH", _detected_chrome))
    _default_linux_profile = Path.home() / "Projects" / "fsl_tracker" / "console_profile"
    if not _default_linux_profile.exists():
        _default_linux_profile = BROWSER_PROFILE_DIR
    CHROME_USER_DATA_DIR = Path(os.environ.get("CHROME_USER_DATA_DIR", _default_linux_profile))

