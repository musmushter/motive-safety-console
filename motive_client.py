import asyncio
import calendar
import datetime
import json
import logging
import os
import re
import subprocess
import time
from typing import Dict, List, Optional, Any

import httpx
import websockets

import config

logger = logging.getLogger("motive_client")

GROUP_MAP = {
    248703: "HEART",
    248702: "ALL AMERICAN",
    248708: "BMW",
    266920: "HOUSTON TOWING",
}


class MotiveClient:
    def __init__(self, headless: bool = False):
        self.headless = headless
        self.is_cdp: bool = False
        self.cached_vehicles: List[Dict[str, Any]] = []
        self.is_busy: bool = False
        self.current_status: str = "Idle"
        self._lock = asyncio.Lock()
        self.driver_mappings: Dict[str, Dict[str, Any]] = {}
        self.offline_vehicle_ids: set = set()
        self.offline_reasons: Dict[str, str] = {}
        self._load_offline_cameras()
        self.camera_blackouts: Dict[str, Any] = self._load_blackouts()
        self.target_config: Dict[str, Any] = self._load_target_config()
        self.web_user_auth_token: Optional[str] = self._load_auth_token()
        self.org2_token: Optional[str] = self._load_org2_auth_token()
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

    def _load_blackouts(self) -> Dict[str, Any]:
        f = config.DATA_DIR / "camera_blackout.json"
        if f.exists():
            try:
                with open(f, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                    logger.info(f"Loaded {len(data)} camera blackout records from {f}.")
                    return data
            except Exception as e:
                logger.warning(f"Could not load camera blackout file: {e}")
        return {}

    def _save_blackouts(self):
        f = config.DATA_DIR / "camera_blackout.json"
        try:
            with open(f, "w", encoding="utf-8") as fp:
                json.dump(self.camera_blackouts, fp, indent=2)
        except Exception as e:
            logger.warning(f"Could not save camera blackout file: {e}")

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

        motion_match = (
            ("transit" in motions and is_transit)
            or ("idle" in motions and is_idle)
            or ("stationary" in motions and is_stationary)
        )
        if not motion_match:
            return False

        if cfg.get("require_camera", True) and not v.get("has_camera"):
            return False

        if cfg.get("exclude_offline", True) and (
            v.get("is_offline")
            or self.is_vehicle_offline(v.get("id"))
            or self.is_vehicle_offline(v.get("name"))
        ):
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
        return None

    def _save_auth_token(self, token: str):
        if not token:
            return
        ht_token = self.org2_token or self._load_org2_auth_token()
        if ht_token and token == ht_token:
            logger.error("Attempted to save Houston Towing token as HEART token! Aborting save.")
            return
        auth_file = config.DATA_DIR / "motive_auth.json"
        try:
            with open(auth_file, "w", encoding="utf-8") as f:
                json.dump({
                    "token": token,
                    "company_name": "HOUSTON EMERGENCY AUTO RESCUE & TRUCKING LLC",
                    "company_id": "260129"
                }, f, indent=2)
        except Exception:
            pass

    def _load_org2_auth_token(self) -> Optional[str]:
        auth_file = config.DATA_DIR / "motive_org2_auth.json"
        if auth_file.exists():
            try:
                with open(auth_file, "r", encoding="utf-8") as f:
                    d = json.load(f)
                    return d.get("token")
            except Exception:
                pass
        return None

    def _save_org2_auth_token(self, token: str):
        if not token:
            return
        heart_token = self.web_user_auth_token or self._load_auth_token()
        if heart_token and token == heart_token:
            logger.error("Attempted to save HEART token as Houston Towing token! Aborting save.")
            return
        auth_file = config.DATA_DIR / "motive_org2_auth.json"
        try:
            with open(auth_file, "w", encoding="utf-8") as f:
                json.dump({
                    "token": token,
                    "company_name": "Houston Towing & Transportation LLC.",
                    "company_id": "599530"
                }, f, indent=2)
        except Exception:
            pass

    def get_token_for_vehicle(self, v: Optional[Dict[str, Any]]) -> Optional[str]:
        if v and (v.get("organization") == "Houston Towing" or str(v.get("org_id")) == "599530"):
            return self.org2_token or self._load_org2_auth_token() or (self.web_user_auth_token or self._load_auth_token())
        return self.web_user_auth_token or self._load_auth_token()

    async def sync_tokens_from_cdp(self) -> Dict[str, str]:
        """
        Scans all open gomotive tabs across all Chrome profiles via CDP and extracts
        x-web-user-auth tokens for HEART and Houston Towing automatically.
        """
        synced = {}
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                r = await client.get("http://127.0.0.1:9222/json")
                if r.status_code != 200:
                    return synced
                targets = [t for t in r.json() if t.get("type") == "page" and "gomotive.com" in t.get("url", "")]

            for t in targets:
                ws_url = t.get("webSocketDebuggerUrl")
                if not ws_url:
                    continue
                try:
                    async with websockets.connect(ws_url) as ws:
                        await ws.send(json.dumps({"id": 1, "method": "Network.getCookies"}))
                        resp = json.loads(await ws.recv())
                        cookies = {c["name"]: c["value"] for c in resp.get("result", {}).get("cookies", [])}
                        auth_token = cookies.get("auth_token")
                        user_id = str(cookies.get("user_id") or "").strip()
                        if auth_token:
                            if user_id not in ("9991540", "9991660"):
                                try:
                                    async with httpx.AsyncClient(timeout=4.0) as val_client:
                                        val_resp = await val_client.get(
                                            "https://api.keeptruckin.com/api/w2/sessions/validate",
                                            headers={"x-web-user-auth": auth_token, "accept": "application/json"}
                                        )
                                        if val_resp.status_code == 200:
                                            user_id = str(val_resp.json().get("user", {}).get("id") or "").strip()
                                except Exception as val_e:
                                    logger.debug(f"Session validate failed during CDP token sync: {val_e}")

                            current_heart = self.web_user_auth_token or self._load_auth_token()
                            current_ht = self.org2_token or self._load_org2_auth_token()

                            if user_id == "9991540":
                                if auth_token != current_ht:
                                    if self.web_user_auth_token != auth_token:
                                        self.web_user_auth_token = auth_token
                                        self._save_auth_token(auth_token)
                                        synced["HEART"] = auth_token
                                        logger.info("Synchronized HEART auth token (user 9991540) from Chrome CDP.")
                                else:
                                    logger.warning("Refusing to assign Houston Towing token to HEART.")
                            elif user_id == "9991660":
                                if auth_token != current_heart:
                                    if self.org2_token != auth_token:
                                        self.org2_token = auth_token
                                        self._save_org2_auth_token(auth_token)
                                        synced["Houston Towing"] = auth_token
                                        logger.info("Synchronized Houston Towing auth token (user 9991660) from Chrome CDP.")
                                else:
                                    logger.warning("Refusing to assign HEART token to Houston Towing.")
                except Exception as e:
                    logger.debug(f"Error reading cookies from target {ws_url}: {e}")
        except Exception as e:
            logger.debug(f"CDP token sync check: {e}")
        return synced

    async def open_vehicle_in_motive(self, vehicle_id: str) -> Dict[str, Any]:
        """
        Opens a vehicle's live map in the dedicated Chrome profile corresponding to its organization:
        - HEART -> Profile 'Default' (user_id 9991540)
        - Houston Towing -> Profile 'Profile 1' (user_id 9991660)
        Brings the target Chrome window and tab directly to the front.
        """
        s_vid = str(vehicle_id).strip()
        v = next((x for x in self.cached_vehicles if str(x.get("id")) == s_vid or str(x.get("numeric_id")) == s_vid or x.get("name") == s_vid), None)

        is_ht = v and (v.get("organization") == "Houston Towing" or str(v.get("org_id")) == "599530")
        target_uid = "9991660" if is_ht else "9991540"
        profile_name = "Profile 1" if is_ht else "Default"
        org_name = "Houston Towing" if is_ht else "HEART"

        num_id = v.get("numeric_id") or v.get("id") if v else s_vid
        target_url = v.get("live_url") or v.get("vehicle_url") if v else None
        if not target_url and num_id:
            target_url = f"https://app.gomotive.com/en-US/#/fleetview/map/vehicle/{num_id}-null-null/{num_id}/live"
        if not target_url:
            target_url = "https://app.gomotive.com/en-US/#/fleetview/map"

        veh_name = v.get("name") if v else s_vid

        # 1. Try navigating an existing Motive tab via CDP
        try:
            async with httpx.AsyncClient(timeout=3.0) as http_client:
                r = await http_client.get("http://127.0.0.1:9222/json")
                if r.status_code == 200:
                    targets = [t for t in r.json() if t.get("type") == "page" and "gomotive.com" in t.get("url", "")]
                    for t in targets:
                        ws_url = t.get("webSocketDebuggerUrl")
                        if not ws_url:
                            continue
                        try:
                            async with websockets.connect(ws_url, close_timeout=2) as ws:
                                await ws.send(json.dumps({"id": 1, "method": "Network.getCookies"}))
                                resp = json.loads(await ws.recv())
                                cookies = {c["name"]: c["value"] for c in resp.get("result", {}).get("cookies", [])}
                                if cookies.get("user_id") == target_uid:
                                    await ws.send(json.dumps({
                                        "id": 2,
                                        "method": "Runtime.evaluate",
                                        "params": {"expression": f'window.location.href = "{target_url}"'}
                                    }))
                                    await ws.recv()
                                    await ws.send(json.dumps({"id": 3, "method": "Page.bringToFront"}))
                                    await ws.recv()
                                    logger.info(f"Navigated {veh_name} via CDP in {org_name} profile ({profile_name}) and brought to front.")
                                    return {
                                        "success": True,
                                        "method": "cdp_navigate",
                                        "vehicle_id": s_vid,
                                        "vehicle_name": veh_name,
                                        "organization": org_name,
                                        "profile": profile_name,
                                        "url": target_url
                                    }
                        except Exception as ce:
                            logger.debug(f"Error checking target {t.get('id')}: {ce}")
        except Exception as e:
            logger.warning(f"CDP navigation error for {veh_name}: {e}")

        # 2. Fallback: Launch Chrome CLI with the exact profile directory
        try:
            cmd = [
                str(config.CHROME_PATH),
                f"--user-data-dir={str(config.CHROME_USER_DATA_DIR)}",
                f"--profile-directory={profile_name}",
                target_url
            ]
            subprocess.Popen(cmd)
            logger.info(f"Launched Chrome CLI for {veh_name} with profile '{profile_name}'.")
            return {
                "success": True,
                "method": "chrome_launch",
                "vehicle_id": s_vid,
                "vehicle_name": veh_name,
                "organization": org_name,
                "profile": profile_name,
                "url": target_url
            }
        except Exception as e:
            logger.error(f"Failed to launch Chrome for {veh_name}: {e}")
            return {
                "success": False,
                "error": str(e),
                "url": target_url,
                "organization": org_name,
                "profile": profile_name
            }

    async def _fetch_motive_vehicles_api(self, timeout: float = 20.0) -> List[Dict[str, Any]]:
        """Queries Motive's REST API in parallel across HEART and Houston Towing organizations."""
        token_heart = self.web_user_auth_token or self._load_auth_token()
        token_ht = self.org2_token or self._load_org2_auth_token()

        if token_heart and token_ht and token_heart == token_ht:
            logger.error("HEART and Houston Towing tokens are IDENTICAL! Skipping duplicate org query.")
            token_ht = None

        if not token_heart and not token_ht:
            return []

        async def _fetch_org(client: httpx.AsyncClient, token: Optional[str], org_name: str, org_id: str):
            if not token:
                return []
            url = "https://api.keeptruckin.com/api/w3/vehicles?per_page=100&page_no=1&sort_field=number&sort_direction=asc&status=active"
            try:
                resp = await client.get(url, headers={
                    "accept": "application/json, text/plain, */*",
                    "x-web-user-auth": token,
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                })
                if resp.status_code == 200:
                    vehs = resp.json().get("vehicles", [])
                    for av in vehs:
                        av["_org_name"] = org_name
                        av["_org_id"] = org_id
                    return vehs
                logger.warning(f"Motive API for {org_name} returned status {resp.status_code}")
            except Exception as e:
                logger.warning(f"Error querying Motive API for {org_name}: {e}")
            return []

        async with httpx.AsyncClient(timeout=timeout) as client:
            res_heart, res_ht = await asyncio.gather(
                _fetch_org(client, token_heart, "HEART", "260129"),
                _fetch_org(client, token_ht, "Houston Towing", "599530")
            )
            return res_heart + res_ht

    async def fetch_live_motion_statuses(self) -> Dict[str, Dict[str, Any]]:
        """
        Actively queries Motive API for real-time motion and driver status for all vehicles
        across both organizations (HEART and Houston Towing) in parallel via async HTTPX.
        """
        try:
            api_vehicles = await self._fetch_motive_vehicles_api(timeout=15.0)
            if not api_vehicles:
                return {}

            motion_map: Dict[str, Dict[str, Any]] = {}
            seen_motion_ids = set()
            for av in api_vehicles:
                v_id = str(av.get("id"))
                if not v_id or v_id in seen_motion_ids:
                    continue
                seen_motion_ids.add(v_id)
                v_num = av.get("number")
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
                    "organization": av.get("_org_name", "HEART"),
                    "org_id": av.get("_org_id", "260129"),
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
                    if m_info.get("organization"):
                        cv["organization"] = m_info["organization"]
                        cv["org_id"] = m_info.get("org_id")
                    if m_info["driver_name"]:
                        cv["driver"] = m_info["driver_name"]
                    if not cv.get("group"):
                        cv["group"] = "Unassigned"

                    if m_info["motion_category"] == "transit":
                        transit_cnt += 1
                    elif m_info["motion_category"] == "idle":
                        idle_cnt += 1

            self.recalculate_targets()
            self.last_motion_pull_ts = time.time()
            logger.info(
                f"Live motion update: {transit_cnt} in transit, {idle_cnt} idling, "
                f"{len(self.cached_vehicles) - transit_cnt - idle_cnt} stationary across {len(self.cached_vehicles)} total vehicles."
            )
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

    def record_camera_strike(self, vid: str, vname: str, reason: str):
        s_vid = str(vid).strip()
        st = self.camera_blackouts.setdefault(s_vid, {"strikes": 0})
        st["strikes"] = st.get("strikes", 0) + 1
        st["last_fail"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        st["reason"] = reason
        st["name"] = vname
        if st["strikes"] >= 2:
            st["no_camera"] = True
            logger.info(f"  {vname} ({vid}): {reason} (strike {st['strikes']}) -> blackout")
            self.flag_offline_vehicle(s_vid, f"Blackout: {reason} ({st['strikes']} strikes)")
            if vname:
                self.flag_offline_vehicle(vname, f"Blackout: {reason} ({st['strikes']} strikes)")
        else:
            logger.info(f"  {vname} ({vid}): {reason} (strike {st['strikes']})")
        self._save_blackouts()

    def clear_camera_strike(self, vid: str, vname: str = ""):
        s_vid = str(vid).strip()
        if s_vid in self.camera_blackouts:
            self.camera_blackouts.pop(s_vid, None)
            self._save_blackouts()
        self.unflag_vehicle(s_vid)
        if vname:
            self.unflag_vehicle(vname)

    def reset_offline_flags(self):
        self.offline_vehicle_ids.clear()
        self.offline_reasons.clear()
        self.camera_blackouts.clear()
        self._save_offline_cameras()
        self._save_blackouts()
        for v in self.cached_vehicles:
            v["is_offline"] = False
            v["offline_reason"] = ""
            if v.get("camera_status") == "Powered Off":
                v["camera_status"] = "Ready to refresh"
        logger.info("Cleared all session offline/powered-off and camera blackout flags.")
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
                        seen_ids = set()
                        deduped = []
                        for v in data:
                            v_id = str(v.get("id") or v.get("name") or "").strip()
                            if not v_id or v_id in seen_ids:
                                continue
                            seen_ids.add(v_id)
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
                            deduped.append(v)
                        self.cached_vehicles = deduped
                        self.save_cached_vehicles()
                        logger.info(f"Loaded {len(self.cached_vehicles)} unique vehicles with associations from {assoc_file}.")
            except Exception as e:
                logger.warning(f"Failed to load local fleet associations: {e}")

    def _load_local_driver_mappings(self):
        """Loads cached driver to Motive URL mappings from data/driver_mappings.json if available."""
        mappings_file = config.DATA_DIR / "driver_mappings.json"
        if mappings_file.exists():
            try:
                with open(mappings_file, "r", encoding="utf-8") as f:
                    self.driver_mappings = json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load local driver mappings: {e}")

    def get_driver_for_vehicle(self, v_num: str, v_id: Optional[str] = None) -> str:
        """Finds driver name from loaded driver mappings if available."""
        if not v_num:
            return "Unassigned"
        for d_name, d_info in self.driver_mappings.items():
            if d_info.get("vehicle_name") == v_num:
                return d_name
        return "Unassigned"

    async def initialize(self):
        """Synchronizes authentication tokens from active Chrome CDP session and fetches initial status."""
        try:
            await self.sync_tokens_from_cdp()
        except Exception as e:
            logger.warning(f"Error during initial CDP token sync: {e}")
        asyncio.create_task(self.fetch_live_motion_statuses())

    def _is_login_url(self, url: str) -> bool:
        url_lower = url.lower()
        return any(term in url_lower for term in [
            "log-in", "login", "sign-in", "signin",
            "account.gomotive.com", "session_expired"
        ])

    async def check_login_status(self) -> Dict[str, Any]:
        """Checks Chrome CDP (port 9222) and local auth tokens to determine Motive session status."""
        await self.sync_tokens_from_cdp()
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                r = await client.get("http://127.0.0.1:9222/json")
                if r.status_code == 200:
                    self.is_cdp = True
                    pages = [t for t in r.json() if t.get("type") == "page" and "gomotive.com" in t.get("url", "")]
                    if pages:
                        for p in pages:
                            u = p.get("url", "")
                            if not self._is_login_url(u):
                                return {"logged_in": True, "url": u, "is_cdp": True, "message": "Connected"}
                        first_url = pages[0].get("url", "")
                        return {"logged_in": False, "url": first_url, "is_cdp": True, "message": "Login required"}
        except Exception:
            self.is_cdp = False

        has_token = bool(self.web_user_auth_token or self.org2_token)
        return {
            "logged_in": has_token,
            "url": config.MOTIVE_URL if has_token else "",
            "is_cdp": self.is_cdp,
            "message": "Connected (cached token)" if has_token else "No Motive tab"
        }

    async def open_login_page(self):
        """Brings an open Motive tab to the front via CDP, or launches Chrome to the Motive login page."""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                r = await client.get("http://127.0.0.1:9222/json")
                if r.status_code == 200:
                    pages = [t for t in r.json() if t.get("type") == "page" and "gomotive.com" in t.get("url", "")]
                    if pages and pages[0].get("webSocketDebuggerUrl"):
                        async with websockets.connect(pages[0]["webSocketDebuggerUrl"], close_timeout=2) as ws:
                            await ws.send(json.dumps({"id": 1, "method": "Page.bringToFront"}))
                            await ws.recv()
                            return
        except Exception:
            pass

        try:
            subprocess.Popen([
                str(config.CHROME_PATH),
                "--remote-debugging-port=9222",
                "--remote-allow-origins=*",
                f"--user-data-dir={config.CHROME_USER_DATA_DIR}",
                "--profile-directory=Default",
                config.MOTIVE_URL
            ])
        except Exception as e:
            logger.error(f"Failed to launch Chrome for login: {e}")

    async def fetch_vehicles_list(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Scans Motive Fleet across all associations (HEART, ALL AMERICAN, BMW, HOUSTON TOWING, and Unassigned).
        Uses Motive's JSON API for instantaneous synchronization.
        """
        if not force_refresh and self.cached_vehicles:
            return self.cached_vehicles

        async with self._lock:
            self.is_busy = True
            self.current_status = "Syncing Motive fleet across all associations..."
            try:
                prev_cache = {str(v.get("id")): v for v in self.cached_vehicles}
                prev_cache.update({str(v.get("name")): v for v in self.cached_vehicles if v.get("name")})

                await self.sync_tokens_from_cdp()
                api_vehicles = await self._fetch_motive_vehicles_api(timeout=25.0)

                if api_vehicles:
                    extracted: List[Dict[str, Any]] = []
                    seen_vehicle_keys = set()
                    for av in api_vehicles:
                        numeric_id = str(av.get("id"))
                        if not numeric_id or numeric_id in seen_vehicle_keys:
                            continue
                        seen_vehicle_keys.add(numeric_id)

                        v_num = av.get("number") or str(numeric_id)
                        make = (av.get("make") or "").strip()
                        model = (av.get("model") or "").strip()
                        year = str(av.get("year") or "").strip()
                        v_mmy = f"{make} {model} {year}".strip()

                        org_name = av.get("_org_name", "HEART")
                        org_id = av.get("_org_id", "260129")

                        g_ids = av.get("group_ids") or []
                        grp_name = "Unassigned"
                        for gid in g_ids:
                            if gid in GROUP_MAP:
                                grp_name = GROUP_MAP[gid]
                                break

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
                            "organization": org_name,
                            "org_id": org_id,
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
                            "eld_identifier": prev.get("eld_identifier") if prev else None,
                            "eld_device_id": av.get("eld_device_id"),
                            "motion_category": motion_category,
                            "speed_mph": speed_mph,
                            "last_motion_update": state_obj.get("entity_state_last_updated") or datetime.datetime.now().isoformat()
                        }
                        self._attach_latest_snapshots(v_dict)
                        v_dict["is_target"] = self.evaluate_is_target(v_dict)
                        extracted.append(v_dict)

                    self.cached_vehicles = extracted
                    self.last_motion_pull_ts = time.time()
                    self.save_cached_vehicles()
                else:
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

    async def get_vehicle_eld(self, vid: str) -> Optional[str]:
        """Resolves and caches the ELD identifier for a given vehicle ID."""
        s_vid = str(vid).strip()
        target_v = next((v for v in self.cached_vehicles if str(v.get("id")) == s_vid or v.get("name") == s_vid), None)
        if target_v and target_v.get("eld_identifier"):
            return target_v["eld_identifier"]

        token = self.get_token_for_vehicle(target_v)
        if not token:
            return None

        url = f"https://api.keeptruckin.com/api/w3/vehicles/{s_vid}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url, headers={
                    "accept": "application/json",
                    "x-web-user-auth": token,
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                })
                if r.status_code == 200:
                    data = r.json()
                    v_data = data.get("vehicle") or data
                    eld = v_data.get("eld_device") or {}
                    ident = eld.get("identifier")
                    if ident:
                        for v in self.cached_vehicles:
                            if str(v.get("id")) == s_vid:
                                v["eld_identifier"] = ident
                                break
                        self.save_cached_vehicles()
                        return ident
        except Exception as e:
            logger.warning(f"Failed to fetch ELD identifier for vehicle {vid}: {e}")
        return None

    async def _refresh_camera_via_api(self, vehicle_id: str, vehicle_name: str, now_iso: str) -> Optional[Dict[str, Any]]:
        """
        Directly triggers Motive hardware live checkin and polls image metadata via REST API.
        """
        s_vid = str(vehicle_id).strip()
        target_v = next((v for v in self.cached_vehicles if str(v.get("id")) == s_vid or v.get("name") == vehicle_name), None)
        token = self.get_token_for_vehicle(target_v)
        if not token:
            logger.info(f"[{vehicle_name}] No x-web-user-auth token available for direct API refresh.")
            return None

        bo = self.camera_blackouts.get(s_vid, {})
        if bo.get("no_camera"):
            logger.info(f"[{vehicle_name}] Camera marked dead/blackout in camera_blackout.json, skipping API refresh.")
            return {
                "vehicle_id": vehicle_id,
                "vehicle_name": vehicle_name,
                "success": False,
                "status_text": "Powered Off",
                "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "timestamp_iso": now_iso,
                "timestamp_ms": int(time.time() * 1000),
                "driver_camera_img": None,
                "road_camera_img": None,
                "motive_camera_time": "Known blackout",
                "is_offline": True,
                "offline_reason": f"Camera marked blackout: {bo.get('reason', 'unresponsive')}",
                "notes": "Skipped due to repeated camera failures."
            }

        if target_v and target_v.get("has_camera") is False:
            return {
                "vehicle_id": vehicle_id,
                "vehicle_name": vehicle_name,
                "success": False,
                "status_text": "Powered Off",
                "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "timestamp_iso": now_iso,
                "timestamp_ms": int(time.time() * 1000),
                "driver_camera_img": None,
                "road_camera_img": None,
                "motive_camera_time": "No dashcam",
                "is_offline": True,
                "offline_reason": "No dashcam installed on vehicle",
                "notes": "No dashcam hardware."
            }

        eld = target_v.get("eld_identifier") if target_v else None
        if not eld:
            eld = await self.get_vehicle_eld(s_vid)

        if not eld:
            logger.warning(f"[{vehicle_name}] Could not resolve ELD device identifier.")
            return {
                "vehicle_id": vehicle_id,
                "vehicle_name": vehicle_name,
                "success": False,
                "status_text": "Powered Off",
                "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "timestamp_iso": now_iso,
                "timestamp_ms": int(time.time() * 1000),
                "driver_camera_img": None,
                "road_camera_img": None,
                "motive_camera_time": "No ELD",
                "is_offline": True,
                "offline_reason": "No ELD device paired with vehicle",
                "notes": "No ELD device."
            }

        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "x-web-user-auth": token,
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                ref_resp = await client.post(
                    "https://api.keeptruckin.com/api/w2/live_checkin/refresh",
                    json={"eld_identifier": eld, "device_ids": []},
                    headers=headers
                )
                if ref_resp.status_code not in (200, 201):
                    logger.warning(f"[{vehicle_name}] Live checkin refresh returned status {ref_resp.status_code}")
                    if ref_resp.status_code in (401, 403):
                        return None
            except Exception as e:
                logger.warning(f"[{vehicle_name}] Live checkin POST failed: {e}")
                return None

            follow_url = f"https://api.keeptruckin.com/api/w3/vehicles/{s_vid}/follow?include_groups=true"
            initial_recv = None
            last_recv = None
            last_cams = {}

            def _img_age_min(recv_iso):
                if not recv_iso:
                    return 9999.0
                try:
                    t = calendar.timegm(time.strptime(recv_iso.replace("Z", ""), "%Y-%m-%dT%H:%M:%S"))
                    return (time.time() - t) / 60.0
                except Exception:
                    return 9999.0

            poll_start = time.time()
            got_fresh = False

            while time.time() - poll_start < 25.0:
                await asyncio.sleep(2.0)
                try:
                    f_resp = await client.get(follow_url, headers=headers)
                    if f_resp.status_code != 200:
                        continue
                    f_data = f_resp.json()
                    v_obj = f_data.get("travel_group", {}).get("vehicle", {})
                    ic = v_obj.get("image_check", {})
                    md = ic.get("last_image_metadata", {})
                    recv_iso = md.get("image_received_time")

                    if initial_recv is None:
                        initial_recv = recv_iso

                    last_recv = recv_iso

                    cams = {}
                    for k in [
                        "driver_facing_image_url", "front_facing_image_url",
                        "front_narrow_facing_image_url", "rear_facing_image_url",
                        "side_left_image_url", "side_right_image_url"
                    ]:
                        if md.get(k):
                            cams[k.replace("_image_url", "")] = md[k]
                    last_cams = cams

                    age = _img_age_min(recv_iso)
                    is_pending = ic.get("pending_image_request", {}).get("active")

                    if (age < 3.0) or (initial_recv and recv_iso and recv_iso != initial_recv):
                        got_fresh = True
                        break
                    if is_pending is False and time.time() - poll_start > 10.0 and cams:
                        break
                except Exception as e:
                    logger.debug(f"[{vehicle_name}] Follow poll exception: {e}")

            driver_img_path = None
            road_img_path = None
            clean_name = re.sub(r"[^a-zA-Z0-9_-]", "_", vehicle_name)
            now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            snapshots_dir = config.SNAPSHOTS_DIR
            snapshots_dir.mkdir(exist_ok=True)

            d_url = last_cams.get("driver_facing")
            r_url = last_cams.get("front_facing") or last_cams.get("front_narrow_facing")

            if d_url:
                try:
                    rd = await client.get(d_url, timeout=10.0)
                    if rd.status_code == 200:
                        fn = f"{clean_name}_driver_{now_str}.jpg"
                        (snapshots_dir / fn).write_bytes(rd.content)
                        driver_img_path = f"/snapshots/{fn}"
                except Exception as e:
                    logger.warning(f"[{vehicle_name}] Driver camera download error: {e}")

            if r_url:
                try:
                    rr = await client.get(r_url, timeout=10.0)
                    if rr.status_code == 200:
                        fn = f"{clean_name}_road_{now_str}.jpg"
                        (snapshots_dir / fn).write_bytes(rr.content)
                        road_img_path = f"/snapshots/{fn}"
                except Exception as e:
                    logger.warning(f"[{vehicle_name}] Road camera download error: {e}")

            age_min = _img_age_min(last_recv)
            is_live = got_fresh or age_min < 3.0

            if is_live and (driver_img_path or road_img_path):
                self.clear_camera_strike(s_vid, vehicle_name)
                logger.info(f"[{vehicle_name}] Direct API refresh succeeded in {time.time() - poll_start:.1f}s (age: {age_min:.1f}m).")
                return {
                    "vehicle_id": vehicle_id,
                    "vehicle_name": vehicle_name,
                    "success": True,
                    "status_text": "Live",
                    "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "timestamp_iso": now_iso,
                    "timestamp_ms": int(time.time() * 1000),
                    "driver_camera_img": driver_img_path,
                    "road_camera_img": road_img_path,
                    "motive_camera_time": "Live",
                    "is_offline": False,
                    "offline_reason": "",
                    "notes": f"Refreshed via Motive API in {time.time() - poll_start:.1f}s."
                }
            else:
                reason = f"No fresh frame after refresh ({age_min:.0f}m old)" if last_recv else "No camera stream"
                self.record_camera_strike(s_vid, vehicle_name, reason)
                status_text = f"{int(age_min)}m ago" if (age_min < 1440 and last_recv) else "Powered Off"
                is_off = (age_min > 35 or not last_cams or self.camera_blackouts.get(s_vid, {}).get("no_camera"))
                logger.info(f"[{vehicle_name}] Camera did not return live frame ({reason}).")
                return {
                    "vehicle_id": vehicle_id,
                    "vehicle_name": vehicle_name,
                    "success": bool(driver_img_path or road_img_path),
                    "status_text": "Powered Off" if is_off else status_text,
                    "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "timestamp_iso": now_iso,
                    "timestamp_ms": int(time.time() * 1000),
                    "driver_camera_img": driver_img_path,
                    "road_camera_img": road_img_path,
                    "motive_camera_time": status_text,
                    "is_offline": is_off,
                    "offline_reason": f"Powered off ({reason})" if is_off else "",
                    "notes": reason
                }

    async def refresh_camera_for_vehicle(self, vehicle_id: str, vehicle_name: str) -> Dict[str, Any]:
        """Refreshes cameras for a vehicle using the direct Motive REST API."""
        now_iso = datetime.datetime.now().isoformat()

        try:
            api_res = await self._refresh_camera_via_api(vehicle_id, vehicle_name, now_iso)
            if api_res is not None:
                for cv in self.cached_vehicles:
                    if str(cv.get("id")) == str(vehicle_id) or cv.get("name") == vehicle_name:
                        if api_res.get("driver_camera_img"):
                            cv["driver_camera_img"] = api_res["driver_camera_img"]
                        if api_res.get("road_camera_img"):
                            cv["road_camera_img"] = api_res["road_camera_img"]
                        cv["camera_status"] = api_res.get("status_text") or cv.get("camera_status")
                        cv["motive_camera_time"] = api_res.get("motive_camera_time") or cv.get("motive_camera_time")
                        cv["last_refreshed"] = api_res.get("timestamp") or cv.get("last_refreshed")
                        cv["last_refreshed_iso"] = api_res.get("timestamp_iso") or cv.get("last_refreshed_iso")
                        cv["last_refreshed_ts"] = api_res.get("timestamp_ms") or cv.get("last_refreshed_ts")
                        cv["is_offline"] = api_res.get("is_offline", False)
                        cv["offline_reason"] = api_res.get("offline_reason", "")
                        break
                self.save_cached_vehicles()
                return api_res
        except Exception as e:
            logger.warning(f"[{vehicle_name}] Direct API refresh exception ({e}).")

        return {
            "vehicle_id": vehicle_id,
            "vehicle_name": vehicle_name,
            "success": False,
            "status_text": "Powered Off",
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "timestamp_iso": now_iso,
            "timestamp_ms": int(time.time() * 1000),
            "driver_camera_img": None,
            "road_camera_img": None,
            "motive_camera_time": "Offline",
            "is_offline": True,
            "offline_reason": "API refresh could not obtain image",
            "notes": "API refresh unavailable"
        }

    async def close(self):
        pass
