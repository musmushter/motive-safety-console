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
CHROME_PATH = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
CHROME_USER_DATA_DIR = Path(r"C:\Users\musta\fsl_tracker\console_profile")
