# Motive Fleet Safety Monitoring Console

A custom dispatch safety monitoring dashboard designed for roadside assistance fleet operations. It automates checking driver-facing and road-facing dashcams on **Motive** (`app.gomotive.com`).

---

## What It Does

1. **Persistent Motive Session**: Keeps you logged in through a persistent browser profile (so you only need to enter 2FA once).
2. **Automatic Vehicle Filtering**:
   - Groups: **HEART** and unassigned / no group.
   - Status: **In Transit** (Moving) and **Idling**.
3. **One-Click Multi-Camera Refresh**:
   - Automatically visits target vehicles and clicks **"Refresh all"** on cameras.
   - Monitors the feed until it reaches **"Live"** status (avoiding stale "15m ago" images).
4. **Unified Dual-Camera Grid**:
   - **Driver-Facing View**: Seatbelt compliance, phone usage, driver present in cab.
   - **Road-Facing View**: Road position (driving in traffic vs pulled over / off-road).
5. **Quick-Review & Logging**:
   - Quick one-click toggles for Seatbelt Violation, Phone Violation, Driver In-Cab, and On-Road.
   - Log compliance checks to a local audit log.
   - One-click **CSV export** for reporting to safety management.
6. **Auto-Refresh**: Optional automated background interval (5, 10, or 15 minutes).

---

## How to Run

### Windows
- **Quick Start**: Double-click `start_console.bat`.
- **Command Line**:
  ```powershell
  .venv\Scripts\activate
  python run.py
  ```

### Linux
- **Quick Start**:
  ```bash
  ./start_console.sh
  ```
- **Stopping the Console on Linux**:
  - Click the **"Stop Server"** button in the top navigation bar of the web dashboard.
  - Or run:
    ```bash
    ./stop_console.sh
    ```

Console URL: [http://localhost:8000](http://localhost:8000)

---

## First-Time Setup (Login)

1. When you first launch the console, the browser window will open to `app.gomotive.com`.
2. Sign in with your standard Motive dispatcher email and password.
3. Complete any two-factor authentication (SMS / Authenticator app) and check "Remember Me".
4. Once you reach the Motive Fleet dashboard, the console is ready. All future launches will remember your login session!
