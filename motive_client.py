import asyncio
import datetime
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Any
import httpx
from playwright.async_api import async_playwright, BrowserContext, Page, Response

import config

def is_live_status(time_text: str) -> bool:
    if not time_text:
        return False
    t = time_text.strip().lower()
    return t in ("live", "just now", "< 1m ago", "0m ago", "<1m ago")

def parse_motive_time_to_unix(time_text: str) -> int:
    """
    Parses Gomotive relative (e.g. '8h 36m ago', '< 1m ago', 'Just now', 'Live')
    or absolute timestamp strings into a UNIX epoch timestamp in milliseconds.
    """
    now_ts = time.time()
    if not time_text:
        return int(now_ts * 1000)
        
    t = time_text.strip()
    if is_live_status(t):
        return int(now_ts * 1000)
        
    m = re.search(r"(\d+)\s*d(?:ays?)?\s*(?:(\d+)\s*h(?:rs?|ours?)?)?\s*ago", t, re.IGNORECASE)
    if m:
        days = int(m.group(1))
        hours = int(m.group(2) or 0)
        return int((now_ts - (days * 86400 + hours * 3600)) * 1000)
        
    m = re.search(r"(\d+)\s*h(?:rs?|ours?)?\s*(?:(\d+)\s*m(?:in|ins|inutes?)?)?\s*ago", t, re.IGNORECASE)
    if m:
        hours = int(m.group(1))
        mins = int(m.group(2) or 0)
        return int((now_ts - (hours * 3600 + mins * 60)) * 1000)
        
    m = re.search(r"(\d+)\s*h(?:rs?|ours?)?\s*ago", t, re.IGNORECASE)
    if m:
        hours = int(m.group(1))
        return int((now_ts - (hours * 3600)) * 1000)
        
    m = re.search(r"(\d+)\s*m(?:in|ins|inutes?)?\s*ago", t, re.IGNORECASE)
    if m:
        mins = int(m.group(1))
        return int((now_ts - (mins * 60)) * 1000)
        
    m = re.search(r"(\d+)\s*s(?:ec|ecs|econds?)?\s*ago", t, re.IGNORECASE)
    if m:
        secs = int(m.group(1))
        return int((now_ts - secs) * 1000)
        
    try:
        dt = datetime.datetime.strptime(t, "%m/%d/%Y %I:%M %p")
        return int(dt.timestamp() * 1000)
    except Exception:
        pass
        
    return int(now_ts * 1000)

def parse_motive_time_to_iso(time_text: str) -> str:
    """Parses Gomotive relative or absolute timestamp into ISO string."""
    ms = parse_motive_time_to_unix(time_text)
    return datetime.datetime.fromtimestamp(ms / 1000.0).isoformat()

logger = logging.getLogger("motive_client")

class MotiveClient:
    def __init__(self, headless: bool = False):
        self.headless = headless
        self.playwright = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.is_cdp: bool = False
        self.cdp_session = None
        self.cached_vehicles: List[Dict[str, Any]] = []
        self.is_busy: bool = False
        self.current_status: str = "Idle"
        self._lock = asyncio.Lock()
        self._claimed_pages = set()
        self.driver_mappings: Dict[str, Dict[str, Any]] = {}
        self.offline_vehicle_ids: set = set()
        self.offline_reasons: Dict[str, str] = {}
        self._load_offline_cameras()
        self.target_config: Dict[str, Any] = self._load_target_config()
        self.web_user_auth_token: Optional[str] = self._load_auth_token()
        self.last_motion_pull_ts: float = 0.0
        self._load_local_driver_mappings()
        self._load_local_fleet_associations()

    def _load_offline_cameras(self):
        f = config.DATA_DIR / "offline_cameras.json"
        if f.exists():
            try:
                with open(f, "r", encoding="utf-8") as fp:
                    d = json.load(fp)
                    self.offline_vehicle_ids = set(d.get("ids", []))
                    self.offline_reasons = d.get("reasons", {})
                    logger.info(f"Loaded {len(self.offline_vehicle_ids)} persisted powered-off cameras from {f}.")
            except Exception as e:
                logger.warning(f"Could not load offline cameras file: {e}")

    def _save_offline_cameras(self):
        f = config.DATA_DIR / "offline_cameras.json"
        try:
            with open(f, "w", encoding="utf-8") as fp:
                json.dump({
                    "ids": list(self.offline_vehicle_ids),
                    "reasons": self.offline_reasons
                }, fp, indent=2)
        except Exception as e:
            logger.warning(f"Could not save offline cameras file: {e}")

    def _load_target_config(self) -> Dict[str, Any]:
        f = config.DATA_DIR / "target_config.json"
        if f.exists():
            try:
                with open(f, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                    if not data.get("associations") or len(data.get("associations", [])) < 3:
                        data["associations"] = ["HEART", "ALL AMERICAN", "BMW", "HOUSTON TOWING", "Unassigned"]
                    return data
            except Exception:
                pass
        return {
            "associations": ["HEART", "ALL AMERICAN", "BMW", "HOUSTON TOWING", "Unassigned"],
            "motions": ["transit", "idle"],
            "require_camera": True,
            "exclude_offline": True
        }

    def save_target_config(self, cfg: Dict[str, Any]):
        self.target_config = cfg
        f = config.DATA_DIR / "target_config.json"
        try:
            with open(f, "w", encoding="utf-8") as fp:
                json.dump(cfg, fp, indent=2)
        except Exception as e:
            logger.warning(f"Could not save target config file: {e}")
        self.recalculate_targets()

    def evaluate_is_target(self, v: Dict[str, Any]) -> bool:
        cfg = self.target_config
        assocs = [a.upper() for a in cfg.get("associations", [])]
        v_grp = (v.get("group") or "Unassigned").upper()
        if assocs and v_grp not in assocs:
            return False

        motions = cfg.get("motions", ["transit", "idle"])
        mot_cat = (v.get("motion_category") or "").lower()
        status_low = (v.get("status") or "").lower()

        is_transit = (mot_cat == "transit" or any(k in status_low for k in ["transit", "moving", "driving"]))
        is_idle = (mot_cat == "idle" or any(k in status_low for k in ["idle", "idling"]))
        is_stationary = not is_transit and not is_idle

        motion_match = False
        if "transit" in motions and is_transit:
            motion_match = True
        if "idle" in motions and is_idle:
            motion_match = True
        if "stationary" in motions and is_stationary:
            motion_match = True
        if not motion_match:
            return False

        if cfg.get("require_camera", True) and not v.get("has_camera"):
            return False

        if cfg.get("exclude_offline", True) and (v.get("is_offline") or self.is_vehicle_offline(v.get("id")) or self.is_vehicle_offline(v.get("name"))):
            return False

        return True

    def recalculate_targets(self):
        for v in self.cached_vehicles:
            v["is_target"] = self.evaluate_is_target(v)

    def _load_auth_token(self) -> Optional[str]:
        auth_file = config.DATA_DIR / "motive_auth.json"
        if auth_file.exists():
            try:
                with open(auth_file, "r", encoding="utf-8") as f:
                    d = json.load(f)
                    return d.get("token")
            except Exception:
                pass
        return "NWU3ZDQxYzNjYjllYTA5MjM3MmNiZmM5YjM3NDViOTUzZDRiMTc3YWNjMWUzYjQwNjExZTZhMDgyMTBkYjA5M2Y0NThhMDBlYzY2ZDkzN2JmNzZmYzNiMzI4Mjk4YmQ2ZjA3MzEyZDJiMTE3YmQ1Mjll"

    def _save_auth_token(self, token: str):
        if not token:
            return
        auth_file = config.DATA_DIR / "motive_auth.json"
        try:
            with open(auth_file, "w", encoding="utf-8") as f:
                json.dump({"token": token}, f)
        except Exception:
            pass

    async def fetch_live_motion_statuses(self) -> Dict[str, Dict[str, Any]]:
        """
        Actively queries Motive API for real-time motion and driver status for all vehicles.
        Executes in <1 second via async HTTPX with the active session token.
        """
        token = self.web_user_auth_token or self._load_auth_token()
        if not token:
            logger.warning("No x-web-user-auth token available for live motion query.")
            return {}

        url = "https://api.keeptruckin.com/api/w3/vehicles?per_page=100&page_no=1&sort_field=number&sort_direction=asc&status=active"
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                resp = await client.get(url, headers={
                    "accept": "application/json, text/plain, */*",
                    "x-web-user-auth": token,
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                })

                if resp.status_code != 200:
                    logger.warning(f"Motive motion API returned status {resp.status_code}")
                    return {}

                data = resp.json()
                api_vehicles = data.get("vehicles", [])

                motion_map: Dict[str, Dict[str, Any]] = {}
                for av in api_vehicles:
                    v_num = av.get("number")
                    v_id = str(av.get("id"))
                    state_obj = av.get("current_state") or {}
                    raw_state = (state_obj.get("entity_state") or "stationary").lower()
                    speed_kph = state_obj.get("ground_speed_kph")
                    speed_mph = round(speed_kph * 0.621371, 1) if (speed_kph is not None and speed_kph > 0) else 0

                    if raw_state in ["driving", "in_transit", "moving"] or speed_mph > 2.0:
                        motion_category = "transit"
                        status_label = f"In Transit ({speed_mph} mph)" if speed_mph > 0 else "In Transit"
                    elif raw_state in ["idling", "idle"]:
                        motion_category = "idle"
                        status_label = "Idling"
                    else:
                        motion_category = "stationary"
                        status_label = "Stationary"

                    driver_obj = av.get("current_driver")
                    driver_name = None
                    if driver_obj and isinstance(driver_obj, dict):
                        fn = driver_obj.get("first_name", "")
                        ln = driver_obj.get("last_name", "")
                        d_full = f"{fn} {ln}".strip()
                        if d_full:
                            driver_name = d_full

                    info = {
                        "id": v_id,
                        "name": v_num,
                        "motion_category": motion_category,
                        "status_label": status_label,
                        "raw_state": raw_state,
                        "speed_mph": speed_mph,
                        "driver_name": driver_name,
                        "located_at": av.get("current_location", {}).get("located_at"),
                        "last_updated": state_obj.get("entity_state_last_updated") or datetime.datetime.now().isoformat()
                    }
                    motion_map[v_id] = info
                    if v_num:
                        motion_map[v_num] = info

                    dash_info = av.get("dashcam_info") or {}
                    if dash_info.get("health_sub_status_key") == "vg_powered_off":
                        if not self.is_vehicle_offline(v_id):
                            self.flag_offline_vehicle(v_id, "Dashcam powered off (reported by Motive)")
                        if v_num and not self.is_vehicle_offline(v_num):
                            self.flag_offline_vehicle(v_num, "Dashcam powered off (reported by Motive)")

                # Update in-memory cached_vehicles
                transit_cnt = 0
                idle_cnt = 0
                for cv in self.cached_vehicles:
                    c_id = str(cv.get("id"))
                    c_name = str(cv.get("name"))
                    m_info = motion_map.get(c_id) or motion_map.get(c_name)
                    if m_info:
                        cv["status"] = m_info["status_label"]
                        cv["motion_category"] = m_info["motion_category"]
                        cv["speed_mph"] = m_info["speed_mph"]
                        cv["last_motion_update"] = m_info["last_updated"]
                        if m_info["driver_name"]:
                            cv["driver"] = m_info["driver_name"]

                        if m_info["motion_category"] == "transit":
                            transit_cnt += 1
                        elif m_info["motion_category"] == "idle":
                            idle_cnt += 1

                self.recalculate_targets()
                self.last_motion_pull_ts = time.time()
                logger.info(f"Live motion update: {transit_cnt} in transit, {idle_cnt} idling, {len(self.cached_vehicles) - transit_cnt - idle_cnt} stationary.")
                return motion_map
        except Exception as e:
            logger.warning(f"Error querying Motive live motion: {e}")
            return {}

    def flag_offline_vehicle(self, identifier: str, reason: str = "Powered off / Unresponsive"):
        if identifier:
            s_id = str(identifier).strip()
            self.offline_vehicle_ids.add(s_id)
            self.offline_reasons[s_id] = reason
            self._save_offline_cameras()
            logger.info(f"Flagged vehicle '{identifier}' as powered-off/dormant: {reason}")
            self.recalculate_targets()

    def unflag_vehicle(self, identifier: str):
        if identifier:
            s_id = str(identifier).strip()
            self.offline_vehicle_ids.discard(s_id)
            self.offline_reasons.pop(s_id, None)
            self._save_offline_cameras()
            self.recalculate_targets()

    def is_vehicle_offline(self, identifier: str) -> bool:
        if not identifier:
            return False
        return str(identifier).strip() in self.offline_vehicle_ids

    def reset_offline_flags(self):
        self.offline_vehicle_ids.clear()
        self.offline_reasons.clear()
        self._save_offline_cameras()
        for v in self.cached_vehicles:
            v["is_offline"] = False
            v["offline_reason"] = ""
            if v.get("camera_status") == "Powered Off":
                v["camera_status"] = "Ready to refresh"
        logger.info("Cleared all session offline/powered-off flags.")
        self.recalculate_targets()

    def _attach_latest_snapshots(self, v: Dict[str, Any]):
        """Finds and attaches the latest saved snapshot images on disk if not already set or missing."""
        v_name = v.get("name", "")
        clean_name = re.sub(r"[^a-zA-Z0-9_-]", "_", v_name)
        sn_dir = config.SNAPSHOTS_DIR
        if not sn_dir.exists():
            return

        try:
            files = os.listdir(sn_dir)
        except Exception:
            return

        d_files = sorted([f for f in files if f.startswith(f"{clean_name}_driver_")], reverse=True)
        if d_files and not v.get("driver_camera_img"):
            v["driver_camera_img"] = f"/snapshots/{d_files[0]}"
            if not v.get("camera_status") or v.get("camera_status") in ["Ready to refresh", "Waiting refresh"]:
                v["camera_status"] = "Live"

        r_files = sorted([f for f in files if f.startswith(f"{clean_name}_road_")], reverse=True)
        if r_files and not v.get("road_camera_img"):
            v["road_camera_img"] = f"/snapshots/{r_files[0]}"
            if not v.get("camera_status") or v.get("camera_status") in ["Ready to refresh", "Waiting refresh"]:
                v["camera_status"] = "Live"

    def save_cached_vehicles(self):
        """Persists self.cached_vehicles to data/fleet_associations.json."""
        assoc_file = config.DATA_DIR / "fleet_associations.json"
        try:
            with open(assoc_file, "w", encoding="utf-8") as f:
                json.dump(self.cached_vehicles, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save fleet_associations.json: {e}")

    def _load_local_fleet_associations(self):
        """Loads verified fleet associations from data/fleet_associations.json if available."""
        assoc_file = config.DATA_DIR / "fleet_associations.json"
        if assoc_file.exists():
            try:
                with open(assoc_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list) and len(data) > 0:
                        for v in data:
                            v_id = str(v.get("id") or v.get("name"))
                            v["is_offline"] = self.is_vehicle_offline(v_id)
                            v["offline_reason"] = self.offline_reasons.get(v_id, "")
                            v["is_target"] = self.evaluate_is_target(v)
                            if "driver_camera_img" not in v:
                                v["driver_camera_img"] = None
                            if "road_camera_img" not in v:
                                v["road_camera_img"] = None
                            if "camera_status" not in v:
                                v["camera_status"] = "Powered Off" if v["is_offline"] else "Ready to refresh"
                            self._attach_latest_snapshots(v)
                        self.cached_vehicles = data
                        self.save_cached_vehicles()
                        logger.info(f"Loaded {len(self.cached_vehicles)} vehicles with associations from {assoc_file}.")
            except Exception as e:
                logger.warning(f"Failed to load local fleet associations: {e}")

    def _load_local_driver_mappings(self):
        """Loads cached driver to Motive URL mappings from data/driver_mappings.json if available."""
        mappings_file = config.DATA_DIR / "driver_mappings.json"
        if mappings_file.exists():
            try:
                with open(mappings_file, "r", encoding="utf-8") as f:
                    self.driver_mappings = json.load(f)
                    logger.info(f"Loaded {len(self.driver_mappings)} local driver mappings from file.")
            except Exception as e:
                logger.warning(f"Failed to load local driver mappings: {e}")

    async def fetch_driver_mappings(self) -> Dict[str, Any]:
        """Fetches driver name to Gomotive driver summary URL mappings from #/fleetview/list/drivers."""
        try:
            if not self.context:
                await self.initialize()

            driver_tab = await self.context.new_page()
            try:
                await driver_tab.goto("https://app.gomotive.com/en-US/#/fleetview/list/drivers", timeout=15000)
                try:
                    await driver_tab.wait_for_selector("table tbody tr td", timeout=8000)
                except Exception:
                    pass
                await asyncio.sleep(2)

                mapping = await driver_tab.evaluate(r"""() => {
                    const rows = Array.from(document.querySelectorAll('table tbody tr'));
                    const map = {};
                    for (const r of rows) {
                        const cells = Array.from(r.querySelectorAll('td'));
                        if (cells.length < 3) continue;
                        const dLink = cells[1]?.querySelector('a');
                        const vLink = cells[2]?.querySelector('a');
                        if (dLink) {
                            const dName = dLink.innerText.trim();
                            const dHref = dLink.href;
                            const vName = vLink ? vLink.innerText.trim() : null;
                            let vHref = vLink ? vLink.href : null;
                            if (vHref && vHref.includes('/vehicles/summary/')) {
                                const vm = vHref.match(/\/vehicles\/summary\/(\d+)/);
                                if (vm) vHref = `https://app.gomotive.com/en-US/#/fleetview/map/vehicle/${vm[1]}-null-null/${vm[1]}/live`;
                            }
                            if (dName && dHref) {
                                map[dName] = { driver_name: dName, driver_url: dHref, vehicle_name: vName, vehicle_url: vHref };
                            }
                        }
                    }
                    return map;
                }""")
                if mapping:
                    self.driver_mappings.update(mapping)
                    mappings_file = config.DATA_DIR / "driver_mappings.json"
                    try:
                        with open(mappings_file, "w", encoding="utf-8") as f:
                            json.dump(self.driver_mappings, f, indent=2)
                    except Exception:
                        pass
                    logger.info(f"Fetched and updated {len(mapping)} driver mappings from Motive.")
            finally:
                await driver_tab.close()
        except Exception as e:
            logger.warning(f"Could not load driver mappings from Motive: {e}")
        return self.driver_mappings

    def get_driver_for_vehicle(self, v_num: str, v_id: Optional[str] = None) -> str:
        """Finds driver name from loaded driver mappings if available."""
        if not v_num:
            return "Unassigned"
        for d_name, d_info in self.driver_mappings.items():
            if d_info.get("vehicle_name") == v_num:
                return d_name
        return "Unassigned"

    async def initialize(self):
        """Connects to active Google Chrome over Remote Debugging (port 9222)."""
        if self.context:
            return

        self.playwright = await async_playwright().start()
        cdp_url = "http://127.0.0.1:9222"
        try:
            logger.info(f"Connecting to Chrome via CDP at {cdp_url}...")
            browser = await self.playwright.chromium.connect_over_cdp(cdp_url)
            self.context = browser.contexts[0]
            self.is_cdp = True
            try:
                self.cdp_session = await browser.new_browser_cdp_session()
            except Exception as e:
                logger.warning(f"Could not create browser CDP session: {e}")
            logger.info("Successfully connected to active Google Chrome!")
            def _on_req(req):
                auth_hdr = req.headers.get("x-web-user-auth")
                if auth_hdr and auth_hdr != self.web_user_auth_token:
                    self.web_user_auth_token = auth_hdr
                    self._save_auth_token(auth_hdr)
            try:
                self.context.on("request", _on_req)
            except Exception:
                pass
            await self._find_or_open_motive_page()
            # Perform initial motion status fetch
            asyncio.create_task(self.fetch_live_motion_statuses())
            return
        except Exception as e:
            logger.warning(f"CDP connection failed ({e}). Falling back to standalone Chrome.")

        args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-infobars",
            "--start-maximized"
        ]
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(config.BROWSER_PROFILE_DIR),
            channel="chrome",
            headless=self.headless,
            viewport=None,
            ignore_default_args=["--enable-automation"],
            args=args,
            accept_downloads=True
        )
        self.is_cdp = False
        await self.context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
        pages = self.context.pages
        self.page = pages[0] if pages else await self.context.new_page()

    async def _find_or_open_motive_page(self) -> Page:
        """Locates the active Motive tab in your Chrome."""
        if not self.context:
            await self.initialize()

        for p in self.context.pages:
            try:
                if "gomotive.com" in p.url:
                    self.page = p
                    return self.page
            except Exception:
                pass

        logger.info("Opening Motive tab in your Chrome...")
        self.page = await self.context.new_page()
        await self.page.goto(config.MOTIVE_URL, wait_until="domcontentloaded")
        return self.page

    def _is_login_url(self, url: str) -> bool:
        url_lower = url.lower()
        return any(term in url_lower for term in [
            "log-in", "login", "sign-in", "signin", 
            "account.gomotive.com", "session_expired"
        ])

    async def check_login_status(self) -> Dict[str, Any]:
        await self._find_or_open_motive_page()
        if not self.page:
            return {"logged_in": False, "url": "", "is_cdp": self.is_cdp, "message": "No Motive tab"}

        current_url = self.page.url
        if self._is_login_url(current_url):
            return {"logged_in": False, "url": current_url, "is_cdp": self.is_cdp, "message": "Login required"}
        if "gomotive.com" in current_url:
            return {"logged_in": True, "url": current_url, "is_cdp": self.is_cdp, "message": "Connected"}

        return {"logged_in": False, "url": current_url, "is_cdp": self.is_cdp, "message": "Navigate to Motive"}

    async def open_login_page(self):
        await self._find_or_open_motive_page()
        if self.page:
            await self.page.bring_to_front()

    async def ensure_heart_group_selected(self) -> bool:
        """Ensures that the Motive dashboard group filter is set to HEART."""
        try:
            current_label = await self.page.evaluate("""() => {
                const l = document.querySelector('.group-filter-custom-label, [data-testid="page-title-container"]');
                return l ? l.innerText.trim() : '';
            }""")
            if "HEART" in current_label and "All Groups" not in current_label:
                logger.info(f"Motive group filter is already set to HEART ({current_label.replace(chr(10), ' ')}).")
                return True

            logger.info("Setting Motive group filter to HEART...")
            await self.page.evaluate("""() => {
                const trigger = document.querySelector('global-groups-multi-select-filter .dropdown-trigger, .group-filter-custom-label');
                if (trigger) trigger.click();
            }""")
            await asyncio.sleep(1)

            clicked = await self.page.evaluate("""() => {
                const items = Array.from(document.querySelectorAll('.cdk-overlay-container phx-select-item, .phx-dropdown phx-select-item, [role="menuitem"], [role="option"]'));
                const heart = items.find(i => i.innerText.trim() === 'HEART');
                if (heart) {
                    heart.click();
                    return true;
                }
                return false;
            }""")
            if clicked:
                await asyncio.sleep(3)
                logger.info("Successfully switched Motive group filter to HEART.")
                return True
            else:
                logger.warning("Could not find HEART in group dropdown.")
                return False
        except Exception as e:
            logger.warning(f"Error selecting HEART group filter: {e}")
            return False

    async def fetch_vehicles_list(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Scans Motive Fleet across all associations (HEART, ALL AMERICAN, BMW, HOUSTON TOWING, and Unassigned).
        Uses Motive's JSON API for instantaneous (<1s), 100% accurate synchronization without DOM scraping bugs.
        Assigns each vehicle and driver their verified association.
        Preserves previously captured camera snapshots and offline states.
        """
        if not force_refresh and self.cached_vehicles:
            return self.cached_vehicles

        async with self._lock:
            self.is_busy = True
            self.current_status = "Syncing Motive fleet across all associations..."
            try:
                prev_cache = {str(v.get("id")): v for v in self.cached_vehicles}
                prev_cache.update({str(v.get("name")): v for v in self.cached_vehicles if v.get("name")})

                token = self.web_user_auth_token or self._load_auth_token()
                GROUP_MAP = {
                    248703: "HEART",
                    248702: "ALL AMERICAN",
                    248708: "BMW",
                }

                api_success = False
                extracted: List[Dict[str, Any]] = []

                if token:
                    try:
                        url = "https://api.keeptruckin.com/api/w3/vehicles?per_page=100&page_no=1&sort_field=number&sort_direction=asc&status=active"
                        async with httpx.AsyncClient(timeout=20.0) as http_client:
                            resp = await http_client.get(url, headers={
                                "accept": "application/json, text/plain, */*",
                                "x-web-user-auth": token,
                                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                            })
                            if resp.status_code == 200:
                                data = resp.json()
                                api_vehicles = data.get("vehicles", [])
                                logger.info(f"Motive API returned {len(api_vehicles)} active fleet vehicles.")

                                for av in api_vehicles:
                                    numeric_id = str(av.get("id"))
                                    v_num = av.get("number") or str(numeric_id)
                                    make = (av.get("make") or "").strip()
                                    model = (av.get("model") or "").strip()
                                    year = str(av.get("year") or "").strip()
                                    v_mmy = f"{make} {model} {year}".strip()

                                    # Map association group
                                    g_ids = av.get("group_ids") or []
                                    grp_name = "Unassigned"
                                    for gid in g_ids:
                                        if gid in GROUP_MAP:
                                            grp_name = GROUP_MAP[gid]
                                            break

                                    # Driver resolution
                                    driver_name = "Unassigned"
                                    driver_obj = av.get("current_driver")
                                    if driver_obj and isinstance(driver_obj, dict):
                                        fn = driver_obj.get("first_name", "")
                                        ln = driver_obj.get("last_name", "")
                                        d_full = f"{fn} {ln}".strip()
                                        if d_full:
                                            driver_name = d_full
                                    if driver_name == "Unassigned":
                                        driver_name = self.get_driver_for_vehicle(v_num, numeric_id)

                                    # Motion & speed
                                    state_obj = av.get("current_state") or {}
                                    raw_state = (state_obj.get("entity_state") or "stationary").lower()
                                    speed_kph = state_obj.get("ground_speed_kph")
                                    speed_mph = round(speed_kph * 0.621371, 1) if (speed_kph is not None and speed_kph > 0) else 0

                                    if raw_state in ["driving", "in_transit", "moving"] or speed_mph > 2.0:
                                        motion_category = "transit"
                                        status_label = f"In Transit ({speed_mph} mph)" if speed_mph > 0 else "In Transit"
                                    elif raw_state in ["idling", "idle"]:
                                        motion_category = "idle"
                                        status_label = "Idling"
                                    else:
                                        motion_category = "stationary"
                                        status_label = "Stationary"

                                    # Camera & health
                                    dash_info = av.get("dashcam_info") or {}
                                    has_camera = bool(av.get("camera") or dash_info)
                                    health_sub = dash_info.get("health_sub_status_key") or ""
                                    health_status = dash_info.get("health_status_key") or ""

                                    is_offline = self.is_vehicle_offline(numeric_id) or self.is_vehicle_offline(v_num)
                                    offline_reason = self.offline_reasons.get(numeric_id) or self.offline_reasons.get(v_num) or ""

                                    if health_sub == "vg_powered_off":
                                        is_offline = True
                                        offline_reason = "Dashcam powered off (reported by Motive)"
                                        self.flag_offline_vehicle(numeric_id, offline_reason)
                                        self.flag_offline_vehicle(v_num, offline_reason)

                                    is_active_cam = has_camera and not is_offline and health_status != "check_power"
                                    cam_color = "#1B6C2D" if is_active_cam else ""

                                    # Preserve snapshot images and refresh history from cache
                                    prev = prev_cache.get(numeric_id) or prev_cache.get(v_num)
                                    driver_img = prev.get("driver_camera_img") if prev else None
                                    road_img = prev.get("road_camera_img") if prev else None
                                    camera_status = "Powered Off" if is_offline else ("Live" if (driver_img or road_img) else "Ready to refresh")
                                    if prev and prev.get("camera_status"):
                                        camera_status = prev["camera_status"]
                                    if is_offline:
                                        camera_status = "Powered Off"

                                    v_dict = {
                                        "id": numeric_id,
                                        "numeric_id": numeric_id,
                                        "name": v_num,
                                        "mmy": v_mmy,
                                        "driver": driver_name,
                                        "group": grp_name,
                                        "status": status_label,
                                        "has_camera": has_camera,
                                        "cam_color": cam_color,
                                        "is_active_cam": is_active_cam,
                                        "summary_href": f"#/fleetview/vehicles/summary/{numeric_id}",
                                        "vehicle_url": f"https://app.gomotive.com/en-US/#/fleetview/map/vehicle/{numeric_id}-null-null/{numeric_id}/live",
                                        "live_url": f"https://app.gomotive.com/en-US/#/fleetview/map/vehicle/{numeric_id}-null-null/{numeric_id}/live",
                                        "is_offline": is_offline,
                                        "offline_reason": offline_reason,
                                        "driver_camera_img": driver_img,
                                        "road_camera_img": road_img,
                                        "camera_status": camera_status,
                                        "last_refreshed_iso": prev.get("last_refreshed_iso") if prev else None,
                                        "last_refreshed_ts": prev.get("last_refreshed_ts") if prev else None,
                                        "motive_camera_time": prev.get("motive_camera_time") if prev else None,
                                        "motion_category": motion_category,
                                        "speed_mph": speed_mph,
                                        "last_motion_update": state_obj.get("entity_state_last_updated") or datetime.datetime.now().isoformat()
                                    }
                                    self._attach_latest_snapshots(v_dict)
                                    v_dict["is_target"] = self.evaluate_is_target(v_dict)
                                    extracted.append(v_dict)

                                api_success = True
                                self.cached_vehicles = extracted
                                self.last_motion_pull_ts = time.time()
                                self.save_cached_vehicles()
                            else:
                                logger.warning(f"Motive API sync returned status {resp.status_code}")
                    except Exception as e:
                        logger.warning(f"Error during direct Motive API sync: {e}")

                if not api_success:
                    logger.info("Using cached vehicles as fallback for sync...")
                    for v in self.cached_vehicles:
                        self._attach_latest_snapshots(v)
                        v["is_target"] = self.evaluate_is_target(v)
                    self.save_cached_vehicles()

                targets_count = len([v for v in self.cached_vehicles if v.get("is_target")])
                self.current_status = f"Synced {len(self.cached_vehicles)} vehicles across all associations ({targets_count} targets ready)."
                return self.cached_vehicles

            except Exception as e:
                logger.error(f"Error fetching vehicles list: {e}")
                self.current_status = f"Error: {str(e)}"
                return self.cached_vehicles
            finally:
                self.is_busy = False

    async def refresh_camera_for_vehicle(self, vehicle_id: str, vehicle_name: str) -> Dict[str, Any]:
        """
        Refreshes cameras for a vehicle in an isolated true background tab with a STRICT 60-second maximum timeout.
        Uses CDP Target.createTarget(background=True) so Chrome NEVER pulls into the foreground or steals focus.
        """
        if not self.context:
            await self.initialize()

        now_iso = datetime.datetime.now().isoformat()
        result = {
            "vehicle_id": vehicle_id,
            "vehicle_name": vehicle_name,
            "success": False,
            "status_text": "Failed",
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "timestamp_iso": now_iso,
            "driver_camera_img": None,
            "road_camera_img": None,
            "notes": ""
        }

        # Find cached vehicle to get its numeric ID
        target_v = next((v for v in self.cached_vehicles if v["name"] == vehicle_name or v["id"] == vehicle_id), None)
        v_num_id = target_v.get("numeric_id") if target_v else vehicle_id
        if not v_num_id:
            m = re.search(r"\d+", vehicle_id)
            v_num_id = m.group(0) if m else vehicle_id

        live_url = f"https://app.gomotive.com/en-US/#/fleetview/map/vehicle/{v_num_id}-null-null/{v_num_id}/live"

        page = None
        try:
            async with self._lock:
                page = await self.context.new_page()

            # Run the camera refresh with 45s safety timeout
            refreshed_result = await asyncio.wait_for(
                self._do_refresh_camera(page, vehicle_id, vehicle_name, live_url, now_iso),
                timeout=45.0
            )

            # Evaluate if camera is live or powered off / dormant
            is_live = bool(
                refreshed_result.get("is_live")
                or is_live_status(refreshed_result.get("status_text", ""))
                or is_live_status(refreshed_result.get("motive_camera_time", ""))
            )
            if is_live:
                self.unflag_vehicle(vehicle_id)
                self.unflag_vehicle(vehicle_name)
                refreshed_result["is_offline"] = False
                refreshed_result["offline_reason"] = ""
            else:
                # Camera did not update to live (powered off or dormant)
                reason = refreshed_result.get("motive_camera_time") or refreshed_result.get("status_text") or "Camera did not update to live"
                self.flag_offline_vehicle(vehicle_id, f"Powered off ({reason})")
                self.flag_offline_vehicle(vehicle_name, f"Powered off ({reason})")
                refreshed_result["is_offline"] = True
                refreshed_result["offline_reason"] = f"Powered off ({reason})"
                refreshed_result["status_text"] = "Powered Off"

            # Directly update cached_vehicles and persist to disk
            for cv in self.cached_vehicles:
                if str(cv.get("id")) == str(vehicle_id) or cv.get("name") == vehicle_name:
                    if refreshed_result.get("driver_camera_img"):
                        cv["driver_camera_img"] = refreshed_result["driver_camera_img"]
                    if refreshed_result.get("road_camera_img"):
                        cv["road_camera_img"] = refreshed_result["road_camera_img"]
                    cv["camera_status"] = refreshed_result.get("status_text") or cv.get("camera_status")
                    cv["motive_camera_time"] = refreshed_result.get("motive_camera_time") or cv.get("motive_camera_time")
                    cv["last_refreshed"] = refreshed_result.get("timestamp") or cv.get("last_refreshed")
                    cv["last_refreshed_iso"] = refreshed_result.get("timestamp_iso") or cv.get("last_refreshed_iso")
                    cv["last_refreshed_ts"] = refreshed_result.get("timestamp_ms") or cv.get("last_refreshed_ts")
                    cv["is_offline"] = refreshed_result.get("is_offline", False)
                    cv["offline_reason"] = refreshed_result.get("offline_reason", "")
                    break
            self.save_cached_vehicles()

            return refreshed_result
        except asyncio.TimeoutError:
            logger.warning(f"Refresh for {vehicle_name} reached 45s timeout limit. Attempting emergency snapshot grab...")
            if page:
                try:
                    em_data = await page.evaluate('''() => {
                        const btn = document.querySelector("[data-testid='refresh-all-btn']");
                        let text = '';
                        if (btn && btn.parentElement) {
                            const span = btn.parentElement.querySelector("span.black-60, span");
                            text = span ? span.innerText.trim() : '';
                        }
                        const d = document.querySelector("img[src*='driver_facing']");
                        const r = document.querySelector("img[src*='front_facing']");
                        return { text, dSrc: d ? d.src : null, rSrc: r ? r.src : null };
                    }''')
                    clean_name = re.sub(r"[^a-zA-Z0-9_-]", "_", vehicle_name)
                    now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    snapshots_dir = config.SNAPSHOTS_DIR
                    snapshots_dir.mkdir(exist_ok=True)
                    async with httpx.AsyncClient() as client:
                        if em_data.get("dSrc"):
                            rd = await client.get(em_data["dSrc"], timeout=8)
                            if rd.status_code == 200:
                                fn = f"{clean_name}_driver_{now_str}.jpg"
                                (snapshots_dir / fn).write_bytes(rd.content)
                                result["driver_camera_img"] = f"/snapshots/{fn}"
                                result["success"] = True
                        if em_data.get("rSrc"):
                            rr = await client.get(em_data["rSrc"], timeout=8)
                            if rr.status_code == 200:
                                fn = f"{clean_name}_road_{now_str}.jpg"
                                (snapshots_dir / fn).write_bytes(rr.content)
                                result["road_camera_img"] = f"/snapshots/{fn}"
                                result["success"] = True
                    em_time = em_data.get("text", "")
                    if em_time and re.search(r"\b(live|just now|< 1m ago|\d+\s*[smhd]|\d+/\d+)\b", em_time, re.I):
                        result["motive_camera_time"] = em_time
                        result["timestamp_ms"] = parse_motive_time_to_unix(em_time)
                        result["timestamp_iso"] = datetime.datetime.fromtimestamp(result["timestamp_ms"] / 1000.0).isoformat()
                        result["status_text"] = em_time
                except Exception as ex:
                    logger.debug(f"Emergency grab error: {ex}")

            self.flag_offline_vehicle(vehicle_id, "Powered off / Timed out (45s)")
            self.flag_offline_vehicle(vehicle_name, "Powered off / Timed out (45s)")
            result["is_offline"] = True
            result["offline_reason"] = "Powered off / Timed out (45s)"
            result["status_text"] = "Powered Off"
            result["notes"] = "Camera did not respond to live checkin."
            return result
        except Exception as e:
            logger.error(f"Error refreshing {vehicle_name}: {e}")
            self.flag_offline_vehicle(vehicle_id, f"Error: {e}")
            self.flag_offline_vehicle(vehicle_name, f"Error: {e}")
            result["is_offline"] = True
            result["offline_reason"] = f"Error: {e}"
            result["status_text"] = "Powered Off"
            result["notes"] = str(e)
            return result
        finally:
            if page:
                async with self._lock:
                    self._claimed_pages.discard(id(page))
                try:
                    await page.close()
                except Exception:
                    pass

    async def _do_refresh_camera(self, page: Page, vehicle_id: str, vehicle_name: str, live_url: str, now_iso: str) -> Dict[str, Any]:
        """Core camera refresh execution logic on a dedicated background page/tab."""
        logger.info(f"[{vehicle_name}] Loading camera view in background...")
        if page.url != live_url and "live" not in page.url:
            await page.goto(live_url, wait_until="domcontentloaded")
            await asyncio.sleep(2.0)

        # 1. Wait up to 18 seconds for the camera drawer and initial frames to load
        driver_url = None
        road_url = None
        initial_driver_url = None
        initial_road_url = None
        cam_time = ""
        is_live = False

        for i in range(18):
            await asyncio.sleep(1.0)
            drawer_data = await page.evaluate('''() => {
                const btn = document.querySelector("[data-testid='refresh-all-btn']");
                let text = '';
                if (btn && btn.parentElement) {
                    const span = btn.parentElement.querySelector("span.black-60, span");
                    text = span ? span.innerText.trim() : '';
                }
                const d = document.querySelector("img[src*='driver_facing']");
                const r = document.querySelector("img[src*='front_facing']");
                
                return {
                    hasBtn: !!btn,
                    text: text,
                    dSrc: d ? d.src : null,
                    rSrc: r ? r.src : null
                };
            }''')

            if drawer_data.get("dSrc"): 
                driver_url = drawer_data["dSrc"]
                initial_driver_url = drawer_data["dSrc"]
            if drawer_data.get("rSrc"): 
                road_url = drawer_data["rSrc"]
                initial_road_url = drawer_data["rSrc"]
            if drawer_data.get("text") and re.search(r"\b(live|just now|< 1m ago|\d+\s*[smhd]|\d+/\d+)\b", drawer_data["text"], re.I):
                cam_time = drawer_data["text"]

            # Trigger refresh instantly via JavaScript DOM evaluate (never hangs!)
            if drawer_data.get("hasBtn"):
                logger.info(f"[{vehicle_name}] Initial Gomotive camera time: '{cam_time}'. Triggering 'Refresh all' via JS...")
                await page.evaluate('''() => {
                    const btn = document.querySelector("[data-testid='refresh-all-btn']");
                    if (btn) btn.click();
                }''')
                break

        # 2. Poll for fresh live image URLs to arrive in the DOM (up to 20 seconds)
        # Note: Gomotive often flips the text indicator to "Live" several seconds BEFORE
        # the <img> tags are actually swapped with the new S3 presigned URLs.
        # We must actively wait for the image URLs (dSrc / rSrc) to change!
        first_change_step = None
        for step in range(40):  # 40 * 0.5s = 20s
            await asyncio.sleep(0.5)
            status_data = await page.evaluate('''() => {
                const btn = document.querySelector("[data-testid='refresh-all-btn']");
                let text = '';
                if (btn && btn.parentElement) {
                    const span = btn.parentElement.querySelector("span.black-60, span");
                    text = span ? span.innerText.trim() : '';
                }
                const d = document.querySelector("img[src*='driver_facing']");
                const r = document.querySelector("img[src*='front_facing']");
                
                return {
                    text: text,
                    dSrc: d ? d.src : null,
                    rSrc: r ? r.src : null
                };
            }''')

            cur_d = status_data.get("dSrc")
            cur_r = status_data.get("rSrc")
            status_text = status_data.get("text", "")

            if cur_d: driver_url = cur_d
            if cur_r: road_url = cur_r
            if status_text and re.search(r"\b(live|just now|< 1m ago|\d+\s*[smhd]|\d+/\d+)\b", status_text, re.I):
                cam_time = status_text

            if is_live_status(status_text):
                is_live = True
                cam_time = "Live"

            # Check whether image URLs have changed relative to pre-refresh initial values
            d_changed = bool(cur_d and cur_d != initial_driver_url)
            r_changed = bool(cur_r and cur_r != initial_road_url)

            if d_changed or r_changed:
                is_live = True
                cam_time = "Live"

            # Case A: Both cameras were initially present
            if initial_driver_url and initial_road_url:
                if d_changed and r_changed:
                    logger.info(f"[{vehicle_name}] Both camera images updated with fresh frames ({step * 0.5:.1f}s)!")
                    break
                elif d_changed or r_changed:
                    if first_change_step is None:
                        first_change_step = step
                    # Wait up to 3 seconds for the second camera to update as well
                    if step - first_change_step >= 6:
                        logger.info(f"[{vehicle_name}] Camera image updated (d_changed={d_changed}, r_changed={r_changed}) ({step * 0.5:.1f}s)!")
                        break

            # Case B: Only one camera was initially present
            elif initial_driver_url or initial_road_url:
                if d_changed or r_changed:
                    logger.info(f"[{vehicle_name}] Fresh camera image updated ({step * 0.5:.1f}s)!")
                    break

            # Case C: Neither camera was initially present
            elif cur_d or cur_r:
                logger.info(f"[{vehicle_name}] Camera images loaded ({step * 0.5:.1f}s)!")
                break

        # 3. Download the snapshots (fresh live frames or current available frames)
        await asyncio.sleep(0.5)
        clean_name = re.sub(r"[^a-zA-Z0-9_-]", "_", vehicle_name)
        timestamp_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        snapshots_dir = config.SNAPSHOTS_DIR
        snapshots_dir.mkdir(exist_ok=True)

        driver_file_path = None
        road_file_path = None
        success = False

        async with httpx.AsyncClient() as client:
            if driver_url:
                try:
                    r = await client.get(driver_url, timeout=10)
                    if r.status_code == 200:
                        fname = f"{clean_name}_driver_{timestamp_str}.jpg"
                        (snapshots_dir / fname).write_bytes(r.content)
                        driver_file_path = f"/snapshots/{fname}"
                        success = True
                except Exception as e:
                    logger.error(f"[{vehicle_name}] Error downloading driver image: {e}")

            if road_url:
                try:
                    r = await client.get(road_url, timeout=10)
                    if r.status_code == 200:
                        fname = f"{clean_name}_road_{timestamp_str}.jpg"
                        (snapshots_dir / fname).write_bytes(r.content)
                        road_file_path = f"/snapshots/{fname}"
                        success = True
                except Exception as e:
                    logger.error(f"[{vehicle_name}] Error downloading road image: {e}")

        # 4. Calculate exact UNIX timestamp (ms) and ISO string based strictly on Gomotive's camera time
        if is_live:
            last_ts = int(time.time() * 1000)
            status_text = "Live"
            cam_time = "Live"
        else:
            last_ts = parse_motive_time_to_unix(cam_time)
            status_text = cam_time or ("Updated" if success else "No Camera Response")

        calculated_iso = datetime.datetime.fromtimestamp(last_ts / 1000.0).isoformat()

        return {
            "vehicle_id": vehicle_id,
            "vehicle_name": vehicle_name,
            "success": success,
            "is_live": is_live,
            "status_text": status_text,
            "motive_camera_time": cam_time or ("Live" if is_live else ""),
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "timestamp_iso": calculated_iso,
            "timestamp_ms": last_ts,
            "driver_camera_img": driver_file_path,
            "road_camera_img": road_file_path,
            "notes": "" if is_live else f"Gomotive image time: {cam_time}"
        }

    async def close(self):
        if self.playwright:
            await self.playwright.stop()
        self.context = None
        self.playwright = None
