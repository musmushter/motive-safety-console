import asyncio
from playwright.async_api import async_playwright

async def click_sf_and_print():
    p = await async_playwright().start()
    browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    context = browser.contexts[0]
    
    motive_page = None
    for page in context.pages:
        if "gomotive.com" in page.url:
            motive_page = page
            break

    sf = motive_page.get_by_text("Show Filters")
    if await sf.count():
        print("Clicking 'Show Filters'...")
        await sf.first.click()
        await asyncio.sleep(1.5)

        # Now print all buttons that appeared in the filter bar
        btns = await motive_page.query_selector_all(".filter-container button, [class*='filter'] button")
        print(f"Filter buttons after click: {len(btns)}")
        for b in btns:
            print("Filter button:", (await b.inner_text()).strip().replace("\n", " "))

    await p.stop()

asyncio.run(click_sf_and_print())
