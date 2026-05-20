import logging

from playwright.async_api import async_playwright

async def create_browser(chromium_cfg: dict):
    """
    Launch explicit executable/channel only when configured, otherwise use
    Playwright's bundled Chromium. Returns (pw, browser).
    """
    logger = logging.getLogger("ntsbot")
    visible_debug = bool(chromium_cfg.get("visible_debug", False))
    headless = False if visible_debug else bool(chromium_cfg.get("headless", False))
    extra_args = chromium_cfg.get("extra_args", []) or []
    use_fake_media = bool(chromium_cfg.get("use_fake_media", True))
    slow_mo = int(chromium_cfg.get("slow_mo_ms", 0) or 0)
    viewport_width = int(
        chromium_cfg.get("viewport_width", chromium_cfg.get("window_width", 1400)) or 1400
    )
    viewport_height = int(
        chromium_cfg.get("viewport_height", chromium_cfg.get("window_height", 900)) or 900
    )

    args = [
        "--disable-dev-shm-usage",
        "--mute-audio",
        "--disable-features=Translate,AutomationControlled",
        "--disable-popup-blocking",
        "--disable-notifications",
        "--disable-extensions",
        "--lang=ru,en",
        f"--window-size={viewport_width},{viewport_height}",
        "--window-position=40,40",
        "--start-maximized",
    ] + extra_args

    if use_fake_media:
        args += ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"]

    channel = chromium_cfg.get("channel") or None
    executable_path = chromium_cfg.get("executable_path") or None
    if executable_path:
        source = "explicit_executable_path"
    elif channel:
        source = "explicit_channel"
    else:
        source = "playwright_bundled_chromium"

    launch_kwargs = {
        "headless": headless,
        "args": args,
    }
    if slow_mo > 0:
        launch_kwargs["slow_mo"] = slow_mo

    logger.info(
        "browser launch: source=%s headless=%s visible_debug=%s executable_path=%s channel=%s slow_mo_ms=%s viewport=%sx%s args=%s",
        source,
        headless,
        visible_debug,
        executable_path,
        channel,
        slow_mo,
        viewport_width,
        viewport_height,
        args,
    )

    pw = await async_playwright().start()

    if executable_path:
        browser = await pw.chromium.launch(
            executable_path=executable_path,
            **launch_kwargs,
        )
    elif channel:
        browser = await pw.chromium.launch(
            channel=channel,
            **launch_kwargs,
        )
    else:
        browser = await pw.chromium.launch(**launch_kwargs)

    return pw, browser
