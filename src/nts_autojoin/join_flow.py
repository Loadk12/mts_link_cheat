from typing import Any, Dict, List

from .healthcheck import is_joined_to_meeting


MIC_SELECTORS = [
    "button[data-testid*=\"Microphone\" i]",
    "button[aria-label*=\"микроф\" i]",
    "button[label*=\"микроф\" i]",
    "[aria-label*=\"microphone\" i]",
]

CAMERA_SELECTORS = [
    "button[data-testid*=\"Camera\" i]",
    "button[data-testid*=\"Video\" i]",
    "button[aria-label*=\"камер\" i]",
    "button[label*=\"камера\" i]",
    "[aria-label*=\"camera\" i]",
    "[aria-label*=\"video\" i]",
]

MIC_ON_HINTS = [
    "выключить микрофон",
    "mute",
    "turn off micro",
    "turn off mic",
]

MIC_OFF_HINTS = [
    "включить микрофон",
    "unmute",
    "turn on micro",
    "turn on mic",
]

CAMERA_ON_HINTS = [
    "выключить камеру",
    "выключить видео",
    "turn off camera",
    "turn video off",
    "stop video",
]

CAMERA_OFF_HINTS = [
    "включить камеру",
    "включить видео",
    "turn on camera",
    "turn video on",
    "start video",
]


async def _maybe_click(page, selector: str):
    try:
        loc = page.locator(selector)
        if await loc.count() > 0:
            await loc.first.click(timeout=1000)
    except Exception:
        pass


async def _click_any(page, selectors: List[str], timeout_ms: int):
    deadline = timeout_ms
    elapsed = 0
    tick = 250
    while elapsed < deadline:
        for selector in selectors:
            try:
                loc = page.locator(selector)
                if await loc.count() > 0:
                    await loc.first.click(timeout=1000)
                    return selector
            except Exception:
                pass
        await page.wait_for_timeout(tick)
        elapsed += tick
    raise TimeoutError(f"click_any: did not find any selector from: {selectors}")


def _log(logger, message: str):
    if logger:
        logger.info(message)


def _step_label(step: Dict[str, Any]) -> str:
    selector = step.get("selector") or ",".join(step.get("selectors") or [])
    return f"{step.get('action')} {selector}".strip()


async def _finish_if_joined(page, logger, label: str) -> bool:
    if await is_joined_to_meeting(page):
        _log(logger, f"detected in-meeting UI after {label}; join success")
        return True
    return False


async def perform_join(page, join_cfg: Dict[str, Any], logger=None):
    """
    Execute configured join steps. If the webinar UI is detected at any point,
    stop immediately and report success instead of waiting for pre-join controls.
    """
    _log(logger, "opening landing page / starting join flow")
    if await _finish_if_joined(page, logger, "initial page"):
        return True

    steps = join_cfg.get("steps", [])
    for step in steps:
        action = step.get("action")
        timeout_ms = int(step.get("timeout_ms", 15000))
        label = _step_label(step)

        if await _finish_if_joined(page, logger, f"before {label}"):
            return True

        if action == "wait":
            selector = step["selector"]
            _log(logger, f"waiting for selector: {selector}")
            await page.wait_for_selector(selector, timeout=timeout_ms)

        elif action == "wait_any":
            selectors: List[str] = step.get("selectors", [])
            if not selectors:
                continue
            _log(logger, f"waiting for one of selectors: {selectors}")
            found = None
            elapsed = 0
            tick = 250
            while elapsed < timeout_ms and not found:
                if await _finish_if_joined(page, logger, "wait_any polling"):
                    return True
                for sel in selectors:
                    try:
                        loc = page.locator(sel)
                        if await loc.count() > 0:
                            found = sel
                            break
                    except Exception:
                        pass
                if found:
                    break
                await page.wait_for_timeout(tick)
                elapsed += tick
            if not found:
                raise TimeoutError(f"wait_any: did not find any selector from: {selectors}")

        elif action == "click":
            selector = step["selector"]
            _log(logger, f"clicking join/control selector: {selector}")
            await page.wait_for_selector(selector, timeout=timeout_ms)
            await page.click(selector)

        elif action == "click_any":
            selectors: List[str] = step.get("selectors", [])
            if not selectors:
                continue
            _log(logger, f"clicking first available selector: {selectors}")
            await _click_any(page, selectors, timeout_ms)

        elif action == "maybe_click":
            selector = step["selector"]
            _log(logger, f"optional click selector: {selector}")
            await _maybe_click(page, selector)

        elif action == "fill":
            selector = step["selector"]
            value = step.get("value", "")
            _log(logger, f"filling field: {selector}")
            await page.wait_for_selector(selector, timeout=timeout_ms)
            await page.fill(selector, value)

        elif action == "maybe_fill":
            selector = step["selector"]
            value = step.get("value", "")
            try:
                loc = page.locator(selector)
                if await loc.count() > 0:
                    await loc.first.fill(value)
            except Exception:
                pass

        elif action == "press":
            selector = step["selector"]
            value = step.get("value", "")
            _log(logger, f"pressing {value} in {selector}")
            await page.wait_for_selector(selector, timeout=timeout_ms)
            await page.press(selector, value)

        elif action == "maybe_press":
            selector = step["selector"]
            value = step.get("value", "")
            try:
                loc = page.locator(selector)
                if await loc.count() > 0:
                    await loc.first.press(value)
            except Exception:
                pass

        elif action == "sleep":
            await page.wait_for_timeout(int(step.get("value", 500)))

        else:
            raise ValueError(f"Unknown action: {action}")

        if await _finish_if_joined(page, logger, f"after {label}"):
            return True

    _log(logger, "join flow steps completed")
    return await is_joined_to_meeting(page)


def _is_on(label: str, on_hints: List[str], off_hints: List[str]) -> bool:
    lower = label.lower()
    if any(h in lower for h in on_hints):
        return True
    if any(h in lower for h in off_hints):
        return False
    return True


async def _ensure_control_off(page, selectors: List[str], on_hints: List[str], off_hints: List[str]):
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if await loc.count() == 0:
                continue

            label = (
                (await loc.get_attribute("aria-label"))
                or (await loc.get_attribute("label"))
                or ""
            )

            if _is_on(label, on_hints, off_hints):
                await loc.click()
                await page.wait_for_timeout(400)

            label_after = (
                (await loc.get_attribute("aria-label"))
                or (await loc.get_attribute("label"))
                or ""
            )
            if _is_on(label_after, on_hints, off_hints):
                await loc.click()
                await page.wait_for_timeout(300)

            return True
        except Exception:
            continue
    return False


async def ensure_media_disabled(page):
    """Try to keep microphone and camera disabled after entering the meeting."""

    await _ensure_control_off(page, MIC_SELECTORS, MIC_ON_HINTS, MIC_OFF_HINTS)
    await _ensure_control_off(page, CAMERA_SELECTORS, CAMERA_ON_HINTS, CAMERA_OFF_HINTS)
