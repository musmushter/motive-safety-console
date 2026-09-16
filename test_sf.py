import asyncio
from playwright.async_api import async_playwright

async def inspect_show_filters():
    p = await async_playwright().start()
    browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    context = browser.contexts[0]
    
    motive_page = None
    for page in context.pages:
        if "gomotive.com" in page.url:
            motive_page = page
            break

    # Look for button "Show Filters"
    sf_btn = motive_page.get_by_text("Show Filters")
    if await sf_btn.count():
        print("Clicking 'Show Filters'...")
        await sf_btn.first.click()
        await asyncio.sleep(2)

        # Print all buttons/comboboxes that appeared
        elements = await motive_page.query_selector_all("button, [role='combobox'], .phx-select, select")
        print(f"Total filter controls: {len(elements)}")
        for el in elements:
            t = (await el.inner_text()).strip().replace("\n", " ")
            if t and any(w in t.lower() for w in ["status", "availability", "motion", "transit", "idle", "filter", "hide", "all"]):
                print("Filter element:", t)

    await p.stop()

asyncio.run(inspect_show_filters())
