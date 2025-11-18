import os, datetime
async def take_screenshot(page, base_dir) -> str:
    os.makedirs(base_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(base_dir, f"screenshot_{ts}.png")
    try:
        await page.screenshot(path=path, full_page=False)
        return path
    except Exception:
        return ""
