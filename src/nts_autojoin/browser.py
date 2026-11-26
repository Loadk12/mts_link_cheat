import json
import logging
from pathlib import Path

from playwright.async_api import async_playwright

from .settings import resolve_project_path


TELEMOST_BLOCKED_SCHEMES = ("telemost", "yandex-telemost", "yandextelemost")


def _launch_settings(chromium_cfg: dict) -> dict:
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
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
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

    return {
        "args": args,
        "channel": channel,
        "executable_path": executable_path,
        "headless": headless,
        "slow_mo": slow_mo,
        "source": source,
        "visible_debug": visible_debug,
        "viewport_width": viewport_width,
        "viewport_height": viewport_height,
    }


def _base_profile_dir(chromium_cfg: dict) -> Path:
    return resolve_project_path(
        chromium_cfg.get("profile_dir")
        or chromium_cfg.get("user_data_dir")
        or "runtime/chrome-profile"
    )


# Профили, которые сейчас заняты запущенным Chrome. Один user-data-dir нельзя
# открыть двумя Chrome одновременно, поэтому параллельные встречи получают
# соседние профили: chrome-profile, chrome-profile-2, chrome-profile-3, ...
_profiles_in_use: set[str] = set()
_context_profiles: dict[int, str] = {}


def _acquire_profile_dir(chromium_cfg: dict) -> Path:
    base = _base_profile_dir(chromium_cfg)
    slot = 1
    while True:
        candidate = base if slot == 1 else base.with_name(f"{base.name}-{slot}")
        key = str(candidate).lower()
        if key not in _profiles_in_use:
            _profiles_in_use.add(key)
            return candidate
        slot += 1


def _release_profile_dir(profile_dir: Path | str) -> None:
    _profiles_in_use.discard(str(profile_dir).lower())


def release_browser_profile(context) -> None:
    """Освободить профиль после закрытия контекста из create_browser_context."""
    if context is None:
        return
    profile_dir = _context_profiles.pop(id(context), None)
    if profile_dir:
        _release_profile_dir(profile_dir)


def _prepare_bot_profile(profile_dir: Path, logger) -> str:
    default_dir = profile_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    prefs_path = default_dir / "Preferences"

    prefs = {}
    if prefs_path.exists():
        try:
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("chrome profile preferences unreadable, recreating: %s", e)
            prefs = {}

    protocol_handler = prefs.setdefault("protocol_handler", {})
    excluded_schemes = protocol_handler.setdefault("excluded_schemes", {})
    for scheme in TELEMOST_BLOCKED_SCHEMES:
        excluded_schemes[scheme] = True

    profile = prefs.setdefault("profile", {})
    content_settings = profile.setdefault("default_content_setting_values", {})
    content_settings.setdefault("media_stream_mic", 1)
    content_settings.setdefault("media_stream_camera", 1)
    content_settings.setdefault("notifications", 2)

    try:
        prefs_path.write_text(json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("chrome profile preferences save failed: %s", e)

    return str(profile_dir)


def _log_launch(logger, settings: dict, profile_dir: str | None = None) -> None:
    logger.info(
        "browser launch: source=%s headless=%s visible_debug=%s executable_path=%s channel=%s slow_mo_ms=%s viewport=%sx%s profile=%s args=%s",
        settings["source"],
        settings["headless"],
        settings["visible_debug"],
        settings["executable_path"],
        settings["channel"],
        settings["slow_mo"],
        settings["viewport_width"],
        settings["viewport_height"],
        profile_dir or "-",
        settings["args"],
    )
    executable_path = settings["executable_path"]
    if executable_path and "program files\\google\\chrome" in executable_path.lower():
        logger.warning(
            "browser launch uses system Google Chrome. For VPN/Happ isolation, use a separate Chrome for Testing executable_path."
        )


async def create_browser(chromium_cfg: dict):
    """
    Launch explicit executable/channel only when configured, otherwise use
    Playwright's bundled Chromium. Returns (pw, browser).
    """
    logger = logging.getLogger("ntsbot")
    settings = _launch_settings(chromium_cfg)

    launch_kwargs = {
        "headless": settings["headless"],
        "args": settings["args"],
    }
    if settings["slow_mo"] > 0:
        launch_kwargs["slow_mo"] = settings["slow_mo"]

    _log_launch(logger, settings)

    pw = await async_playwright().start()

    if settings["executable_path"]:
        browser = await pw.chromium.launch(
            executable_path=settings["executable_path"],
            **launch_kwargs,
        )
    elif settings["channel"]:
        browser = await pw.chromium.launch(
            channel=settings["channel"],
            **launch_kwargs,
        )
    else:
        browser = await pw.chromium.launch(**launch_kwargs)

    return pw, browser


async def create_browser_context(chromium_cfg: dict):
    """
    Launch the bot browser with a persistent profile. The persistent profile lets
    Chrome keep protocol/content preferences identical in visible debug and NSSM.
    Returns (pw, browser, context).
    """
    logger = logging.getLogger("ntsbot")
    settings = _launch_settings(chromium_cfg)
    profile_path = _acquire_profile_dir(chromium_cfg)
    try:
        return await _launch_with_profile(chromium_cfg, settings, profile_path, logger)
    except BaseException:
        _release_profile_dir(profile_path)
        raise


async def _launch_with_profile(chromium_cfg: dict, settings: dict, profile_path: Path, logger):
    profile_dir = _prepare_bot_profile(profile_path, logger)

    launch_kwargs = {
        "headless": settings["headless"],
        "args": settings["args"],
        "viewport": None
        if settings["visible_debug"]
        else {"width": settings["viewport_width"], "height": settings["viewport_height"]},
    }
    if settings["slow_mo"] > 0:
        launch_kwargs["slow_mo"] = settings["slow_mo"]

    _log_launch(logger, settings, profile_dir=profile_dir)
    logger.info("browser profile: external protocol blocked_schemes=%s", list(TELEMOST_BLOCKED_SCHEMES))

    pw = await async_playwright().start()

    try:
        if settings["executable_path"]:
            context = await pw.chromium.launch_persistent_context(
                profile_dir,
                executable_path=settings["executable_path"],
                **launch_kwargs,
            )
        elif settings["channel"]:
            context = await pw.chromium.launch_persistent_context(
                profile_dir,
                channel=settings["channel"],
                **launch_kwargs,
            )
        else:
            context = await pw.chromium.launch_persistent_context(profile_dir, **launch_kwargs)
    except BaseException:
        try:
            await pw.stop()
        except Exception:
            pass
        raise

    _context_profiles[id(context)] = str(profile_path)
    return pw, context.browser, context
