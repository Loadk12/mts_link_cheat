from playwright.async_api import async_playwright

async def create_browser(chromium_cfg: dict):
    """
    Запускает именно Google Chrome (если задан executable_path или channel),
    иначе — падает назад на встроенный Chromium.
    Возвращает (pw, browser).
    """
    headless = bool(chromium_cfg.get("headless", False))
    extra_args = chromium_cfg.get("extra_args", []) or []
    use_fake_media = bool(chromium_cfg.get("use_fake_media", True))

    args = [
        "--disable-dev-shm-usage",
        "--mute-audio",
        "--disable-features=Translate,AutomationControlled",
        "--disable-popup-blocking",
        "--disable-notifications",
        "--disable-extensions",
        "--lang=ru,en",
        # "--no-sandbox",  # для Windows обычно не требуется
    ] + extra_args

    if use_fake_media:
        args += ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"]

    channel = chromium_cfg.get("channel")                 # напр. "chrome"
    executable_path = chromium_cfg.get("executable_path") # полный путь к chrome.exe

    pw = await async_playwright().start()

    if executable_path:
        browser = await pw.chromium.launch(
            headless=headless,
            executable_path=executable_path,
            args=args,
        )
    elif channel:
        browser = await pw.chromium.launch(
            headless=headless,
            channel=channel,
            args=args,
        )
    else:
        # Фоллбэк — встроенный Chromium (если Chrome недоступен)
        browser = await pw.chromium.launch(
            headless=headless,
            args=args,
        )

    return pw, browser
