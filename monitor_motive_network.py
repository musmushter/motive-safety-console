import asyncio
import json
import time
import sys
import httpx
from pathlib import Path
from playwright.async_api import async_playwright

sys.stdout.reconfigure(line_buffering=True)

async def check_token_company(token: str):
    headers = {
        "accept": "application/json, text/plain, */*",
        "x-web-user-auth": token,
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            # Try fetching vehicles to see company and count
            res = await client.get(
                "https://api.keeptruckin.com/api/w3/vehicles?per_page=5&page_no=1&sort_field=number&sort_direction=asc&status=active",
                headers=headers
            )
            if res.status_code == 200:
                data = res.json()
                total = data.get("pagination", {}).get("total", len(data.get("vehicles", [])))
                vehicles = data.get("vehicles", [])
                sample_names = [v.get("number") for v in vehicles[:3]]
                return {
                    "valid": True,
                    "total_vehicles": total,
                    "sample_vehicles": sample_names
                }
    except Exception as e:
        print(f"Error checking token company: {e}")
    return {"valid": False}

async def monitor():
    print("=" * 60)
    print("MOTIVE ORGANIZATION NETWORK LISTENER")
    print("=" * 60)
    print("Connecting to Chrome on port 9222...")
    
    p = await async_playwright().start()
    browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = browser.contexts[0]

    captured_tokens = {}
    out_all = Path("data/captured_org_tokens.json")
    primary_token_file = Path("data/motive_auth.json")
    org2_token_file = Path("data/motive_org2_auth.json")

    primary_token = None
    if primary_token_file.exists():
        try:
            with open(primary_token_file, "r") as f:
                primary_token = json.load(f).get("token")
        except Exception:
            pass

    async def on_request(req):
        url = req.url
        auth = req.headers.get("x-web-user-auth")
        if not auth:
            return

        if auth not in captured_tokens:
            captured_tokens[auth] = {
                "token": auth,
                "first_seen_url": url,
                "timestamp": time.time()
            }
            is_primary = (auth == primary_token)
            print(f"\n>>> Intercepted token: {auth[:25]}... (Primary: {is_primary})")
            print(f"    From URL: {url[:100]}")

            # Verify token via API
            info = await check_token_company(auth)
            if info.get("valid"):
                total = info.get("total_vehicles")
                samples = info.get("sample_vehicles")
                print(f"    [API Verified] Total vehicles: {total}, Samples: {samples}")
                captured_tokens[auth]["info"] = info

                if not is_primary:
                    print("\n" + "*" * 60)
                    print(f"SUCCESS! SECOND ORGANIZATION TOKEN CAPTURED!")
                    print(f"Vehicles found: {total} (e.g. {samples})")
                    print("*" * 60 + "\n")
                    with open(org2_token_file, "w") as f:
                        json.dump({
                            "token": auth,
                            "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "total_vehicles": total,
                            "sample_vehicles": samples
                        }, f, indent=2)

            with open(out_all, "w") as f:
                json.dump(captured_tokens, f, indent=2)

    # Listen on context requests (Playwright handles async callbacks cleanly)
    ctx.on("request", lambda req: asyncio.create_task(on_request(req)))

    print("Listener is ACTIVE and waiting for organization switch in Chrome.")
    print("Switch to the other organization in your Chrome tab now...")
    print("=" * 60)

    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        await browser.close()
        await p.stop()

if __name__ == "__main__":
    try:
        asyncio.run(monitor())
    except KeyboardInterrupt:
        print("\nListener stopped.")
