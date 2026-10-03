import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from motive_client import MotiveClient

logger = logging.getLogger("server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = FastAPI(title="Motive Safety Monitoring Console")

# Mount snapshots and static files
app.mount("/snapshots", StaticFiles(directory=str(config.SNAPSHOTS_DIR)), name="snapshots")
app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")

motive_client = MotiveClient(headless=False)
motion_task: Optional[asyncio.Task] = None

async def motion_polling_loop():
    """Actively pulls motion status from Motive every 60 seconds in the background."""
    logger.info("Motion polling loop started (interval: 60s).")
    while True:
        await asyncio.sleep(60)
        try:
            await motive_client.fetch_live_motion_statuses()
        except Exception as e:
            logger.warning(f"Error in motion polling loop: {e}")

class RefreshRequest(BaseModel):
    vehicle_id: Optional[str] = None
    vehicle_name: Optional[str] = None

@app.on_event("startup")
async def startup_event():
    global motion_task
    logger.info("Starting up Motive Client...")
    try:
        await motive_client.initialize()
    except Exception as e:
        logger.error(f"Failed to auto-init browser: {e}")
    # Immediately pull live motion statuses once so targets and motion categories are populated from the start
    try:
        await motive_client.fetch_live_motion_statuses()
    except Exception as e:
        logger.warning(f"Initial live motion fetch on startup: {e}")
    motion_task = asyncio.create_task(motion_polling_loop())

@app.on_event("shutdown")
async def shutdown_event():
    global motion_task
    if motion_task:
        motion_task.cancel()
    logger.info("Shutting down Motive Client...")
    await motive_client.close()

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = config.STATIC_DIR / "index.html"
    if not index_path.exists():
        return HTMLResponse("<h1>Loading Console...</h1>")
    with open(index_path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.get("/api/status")
async def get_status():
    login_info = await motive_client.check_login_status()
    motive_client.recalculate_targets()
    targets = [v for v in motive_client.cached_vehicles if v.get("is_target")]
    offline_count = len([v for v in targets if v.get("is_offline")])
    active_count = len(targets) - offline_count
    heart_count = len([v for v in motive_client.cached_vehicles if v.get("organization") == "HEART"])
    ht_count = len([v for v in motive_client.cached_vehicles if v.get("organization") == "Houston Towing"])
    return {
        "logged_in": login_info["logged_in"],
        "url": login_info["url"],
        "is_busy": motive_client.is_busy,
        "current_status": motive_client.current_status,
        "total_cached_vehicles": len(motive_client.cached_vehicles),
        "organizations": ["HEART", "Houston Towing"],
        "org_counts": {"HEART": heart_count, "Houston Towing": ht_count},
        "target_count": len(targets),
        "active_count": active_count,
        "offline_count": offline_count
    }

@app.post("/api/login")
async def open_login():
    """Opens Motive web app so user can sign in or complete 2FA."""
    await motive_client.open_login_page()
    return {"message": "Motive browser window brought to front. Please sign in."}

@app.get("/api/vehicles")
async def get_vehicles(refresh: bool = False):
    """Fetches vehicles across all associations. If refresh=True or cache empty, queries Motive page."""
    if refresh or not motive_client.cached_vehicles:
        vehicles = await motive_client.fetch_vehicles_list(force_refresh=refresh)
    else:
        vehicles = motive_client.cached_vehicles
    
    # Always ensure live motion is populated and targets recalculated on refresh
    if refresh or not motive_client.last_motion_pull_ts:
        try:
            await motive_client.fetch_live_motion_statuses()
        except Exception as e:
            logger.warning(f"Live motion fetch in get_vehicles: {e}")

    # Re-evaluate targets with current target config and live motion
    motive_client.recalculate_targets()

    # Deduplicate vehicles by unique ID
    deduped_v = []
    seen_vids = set()
    for v in motive_client.cached_vehicles:
        vid = str(v.get("id") or v.get("numeric_id") or v.get("name") or "").strip()
        if vid and vid not in seen_vids:
            seen_vids.add(vid)
            deduped_v.append(v)
    vehicles = deduped_v
    motive_client.cached_vehicles = vehicles

    targets = [v for v in vehicles if v.get("is_target")]
    others = [v for v in vehicles if not v.get("is_target")]

    # Associations / Groups breakdown
    groups = ["HEART", "ALL AMERICAN", "BMW", "HOUSTON TOWING", "Unassigned"]
    group_counts = {g: 0 for g in groups}
    for v in vehicles:
        grp = v.get("group") or "Unassigned"
        matched = False
        for g in groups:
            if grp.upper() == g.upper():
                group_counts[g] += 1
                matched = True
                break
        if not matched:
            group_counts["Unassigned"] += 1

    org_counts = {
        "All": len(vehicles),
        "HEART": len([v for v in vehicles if v.get("organization") == "HEART"]),
        "Houston Towing": len([v for v in vehicles if v.get("organization") == "Houston Towing"])
    }

    return {
        "vehicles": vehicles,
        "targets": targets,
        "others": others,
        "groups": groups,
        "group_counts": group_counts,
        "organizations": ["HEART", "Houston Towing"],
        "org_counts": org_counts,
        "total": len(vehicles)
    }

@app.get("/api/vehicles/motion")
async def get_motion_status():
    """Actively returns real-time motion statuses, categories, and speeds for all fleet vehicles."""
    if time.time() - motive_client.last_motion_pull_ts > 10:
        await motive_client.fetch_live_motion_statuses()

    motion_summary = {}
    for v in motive_client.cached_vehicles:
        v_id = str(v.get("id") or v.get("name"))
        motion_summary[v_id] = {
            "id": v_id,
            "name": v.get("name"),
            "status": v.get("status"),
            "motion_category": v.get("motion_category", "stationary"),
            "speed_mph": v.get("speed_mph", 0),
            "driver": v.get("driver"),
            "last_motion_update": v.get("last_motion_update")
        }
    return {
        "timestamp": datetime.now().isoformat(),
        "motion": motion_summary,
        "total": len(motion_summary)
    }

@app.post("/api/vehicles/refresh")
async def refresh_camera(req: RefreshRequest):
    """Triggers snapshot refresh for a single vehicle."""
    if not req.vehicle_name:
        raise HTTPException(status_code=400, detail="vehicle_name required")
    
    res = await motive_client.refresh_camera_for_vehicle(req.vehicle_id or req.vehicle_name, req.vehicle_name)
    
    # Update cache with latest snapshot data
    now_iso = res.get("timestamp_iso") or datetime.now().isoformat()
    for v in motive_client.cached_vehicles:
        if v.get("name") == req.vehicle_name or v.get("id") == req.vehicle_id:
            v["driver_camera_img"] = res.get("driver_camera_img") or v.get("driver_camera_img")
            v["road_camera_img"] = res.get("road_camera_img") or v.get("road_camera_img")
            v["camera_status"] = res.get("status_text")
            v["motive_camera_time"] = res.get("motive_camera_time")
            v["last_refreshed"] = res.get("timestamp")
            v["last_refreshed_iso"] = now_iso
            v["last_refreshed_ts"] = res.get("timestamp_ms")
            v["is_offline"] = res.get("is_offline", False)
            v["offline_reason"] = res.get("offline_reason", "")

    return res

@app.post("/api/vehicles/reset-offline")
async def reset_offline_flags():
    """Resets session offline/powered-off flags so all cameras can be tried again."""
    motive_client.reset_offline_flags()
    return {"status": "ok", "message": "All powered-off flags cleared for this session."}

class TargetConfigRequest(BaseModel):
    associations: Optional[List[str]] = None
    motions: Optional[List[str]] = None
    require_camera: Optional[bool] = None
    exclude_offline: Optional[bool] = None

@app.get("/api/targets/config")
async def get_target_config():
    """Returns current target rule configuration and vehicle counts."""
    target_count = len([v for v in motive_client.cached_vehicles if v.get("is_target")])
    return {
        "config": motive_client.target_config,
        "target_count": target_count,
        "total_vehicles": len(motive_client.cached_vehicles)
    }

@app.post("/api/targets/config")
async def update_target_config(req: TargetConfigRequest):
    """Updates target criteria rules (associations, motion states, camera requirements)."""
    cfg = dict(motive_client.target_config)
    if req.associations is not None:
        cfg["associations"] = req.associations
    if req.motions is not None:
        cfg["motions"] = req.motions
    if req.require_camera is not None:
        cfg["require_camera"] = req.require_camera
    if req.exclude_offline is not None:
        cfg["exclude_offline"] = req.exclude_offline
    motive_client.save_target_config(cfg)
    target_count = len([v for v in motive_client.cached_vehicles if v.get("is_target")])
    return {
        "status": "ok",
        "config": cfg,
        "target_count": target_count
    }

class OpenInMotiveRequest(BaseModel):
    vehicle_id: str

@app.post("/api/vehicles/open-in-motive")
async def open_vehicle_in_motive_route(req: OpenInMotiveRequest):
    """Opens vehicle live map in the dedicated profile window (Default for HEART, Profile 1 for Houston Towing)."""
    result = await motive_client.open_vehicle_in_motive(req.vehicle_id)
    return result

batch_refresh_cancel_requested = False

@app.post("/api/vehicles/refresh-all/stop")
async def stop_refresh_all():
    global batch_refresh_cancel_requested
    batch_refresh_cancel_requested = True
    motive_client.is_busy = False
    motive_client.current_status = "Batch refresh stopped by user."
    return {"status": "stopped", "message": "Batch camera refresh stopped."}

@app.post("/api/vehicles/refresh-all")
async def refresh_all_cameras(background_tasks: BackgroundTasks, concurrency: int = 6, force: bool = False, restart: bool = False):
    """Triggers parallel batch camera refresh for target vehicles, skipping powered-off and stationary vehicles unless targeted."""
    global batch_refresh_cancel_requested
    if motive_client.is_busy:
        if restart:
            batch_refresh_cancel_requested = True
            await asyncio.sleep(0.5)
            motive_client.is_busy = False
        else:
            return {
                "status": "busy",
                "message": "Client is already running a batch refresh. Click 'Stop' or wait for it to complete."
            }

    # Ensure targets are accurately evaluated
    motive_client.recalculate_targets()
    all_targets = [v for v in motive_client.cached_vehicles if v.get("is_target")]

    if force:
        motive_client.reset_offline_flags()

    # Determine whether user target configuration explicitly includes stationary vehicles
    cfg_motions = motive_client.target_config.get("motions", ["transit", "idle"])
    allow_stationary = "stationary" in cfg_motions or force

    targets = []
    skipped_offline = 0
    skipped_stationary = 0
    for v in all_targets:
        if not force and v.get("is_offline"):
            skipped_offline += 1
            continue
        mot_cat = (v.get("motion_category") or "").lower()
        status_low = (v.get("status") or "").lower()
        is_stationary = (mot_cat == "stationary") or (not any(k in status_low for k in ["transit", "moving", "driving", "idle", "idling"]))
        if not allow_stationary and is_stationary:
            skipped_stationary += 1
            continue
        targets.append(v)

    if not targets:
        skip_reasons = []
        if skipped_offline:
            skip_reasons.append(f"{skipped_offline} powered off")
        if skipped_stationary:
            skip_reasons.append(f"{skipped_stationary} stationary/parked")
        skip_msg = f" (Skipped: {', '.join(skip_reasons)})" if skip_reasons else ""
        return {
            "status": "skipped",
            "count": 0,
            "skipped_offline": skipped_offline,
            "skipped_stationary": skipped_stationary,
            "message": f"No eligible target vehicles to refresh{skip_msg}. Use 'Retry All' to test offline units or include stationary in target settings."
        }

    pool_size = max(1, min(concurrency, 8))

    async def _parallel_batch_refresh(vehicle_list, pool, skipped_off, skipped_stat):
        global batch_refresh_cancel_requested
        batch_refresh_cancel_requested = False
        motive_client.is_busy = True
        total = len(vehicle_list)
        completed = 0
        sem = asyncio.Semaphore(pool)
        start_ts = datetime.now()

        skip_parts = []
        if skipped_stat > 0:
            skip_parts.append(f"{skipped_stat} stationary")
        if skipped_off > 0:
            skip_parts.append(f"{skipped_off} powered-off")
        skip_str = f" (Skipped {', '.join(skip_parts)})" if skip_parts else ""
        motive_client.current_status = f"Starting refresh for {total} active target vehicles...{skip_str}"

        async def _worker(idx, v):
            nonlocal completed
            if batch_refresh_cancel_requested:
                return
            if idx > 0 and idx < pool:
                await asyncio.sleep(idx * 0.2)
            async with sem:
                if batch_refresh_cancel_requested:
                    return
                v_name = v.get("name", "Unknown")
                v_id = v.get("id") or v_name
                try:
                    res = await motive_client.refresh_camera_for_vehicle(v_id, v_name)
                    v["driver_camera_img"] = res.get("driver_camera_img") or v.get("driver_camera_img")
                    v["road_camera_img"] = res.get("road_camera_img") or v.get("road_camera_img")
                    v["camera_status"] = res.get("status_text")
                    v["motive_camera_time"] = res.get("motive_camera_time")
                    v["last_refreshed"] = res.get("timestamp")
                    v["last_refreshed_iso"] = res.get("timestamp_iso") or v.get("last_refreshed_iso")
                    v["last_refreshed_ts"] = res.get("timestamp_ms")
                    v["is_offline"] = res.get("is_offline", False)
                    v["offline_reason"] = res.get("offline_reason", "")

                    # Keep motive_client.cached_vehicles synced in real time
                    for cv in motive_client.cached_vehicles:
                        if str(cv.get("id")) == str(v_id) or cv.get("name") == v_name:
                            cv["driver_camera_img"] = v["driver_camera_img"]
                            cv["road_camera_img"] = v["road_camera_img"]
                            cv["camera_status"] = v["camera_status"]
                            cv["motive_camera_time"] = v["motive_camera_time"]
                            cv["last_refreshed"] = v["last_refreshed"]
                            cv["last_refreshed_iso"] = v["last_refreshed_iso"]
                            cv["last_refreshed_ts"] = v["last_refreshed_ts"]
                            cv["is_offline"] = v["is_offline"]
                            cv["offline_reason"] = v["offline_reason"]
                            break
                    motive_client.save_cached_vehicles()
                except Exception as e:
                    logger.error(f"Worker error for {v_name}: {e}")
                    v["camera_status"] = "Powered Off"
                    v["is_offline"] = True
                    v["offline_reason"] = str(e)
                finally:
                    completed += 1
                    if not batch_refresh_cancel_requested:
                        motive_client.current_status = f"Refreshing fleet: {completed}/{total} completed..."

        try:
            tasks = [_worker(i, v) for i, v in enumerate(vehicle_list)]
            await asyncio.gather(*tasks)
            if not batch_refresh_cancel_requested:
                elapsed_sec = int((datetime.now() - start_ts).total_seconds())
                offline_count = len([v for v in all_targets if v.get("is_offline")])
                motive_client.current_status = f"Batch refresh complete in {elapsed_sec}s! ({total} active refreshed, {offline_count} powered off/skipped)."
        finally:
            motive_client.is_busy = False

    background_tasks.add_task(_parallel_batch_refresh, targets, pool_size, skipped_offline, skipped_stationary)
    skip_summary = []
    if skipped_offline > 0:
        skip_summary.append(f"{skipped_offline} powered-off")
    if skipped_stationary > 0:
        skip_summary.append(f"{skipped_stationary} stationary")
    skip_text = f" ({', '.join(skip_summary)} skipped)" if skip_summary else ""

    return {
        "status": "started",
        "count": len(targets),
        "skipped_offline": skipped_offline,
        "skipped_stationary": skipped_stationary,
        "concurrency": pool_size,
        "message": f"Started refreshing {len(targets)} active vehicles{skip_text} in background."
    }
