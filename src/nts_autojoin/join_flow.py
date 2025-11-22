from typing import Dict, Any, List


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

async def _exists(page, selector: str) -> bool:
    try:
        loc = page.locator(selector)
        return (await loc.count()) > 0
    except Exception:
        return False

async def _maybe_click(page, selector: str):
    try:
        loc = page.locator(selector)
        if await loc.count() > 0:
            await loc.first().click()
    except Exception:
        pass

async def perform_join(page, join_cfg: Dict[str, Any]):
    """
    Выполняет шаги из конфигурации.
    Действия:
      - wait:        {action, selector, timeout_ms}
      - wait_any:    {action, selectors: [..], timeout_ms}
      - click:       {action, selector, timeout_ms}
      - maybe_click: {action, selector}
      - fill:        {action, selector, value, timeout_ms}
      - maybe_fill:  {action, selector, value}
      - press:       {action, selector, value, timeout_ms}
      - maybe_press: {action, selector, value}
      - sleep:       {action, value}
    """
    steps = join_cfg.get("steps", [])
    for step in steps:
        action = step.get("action")
        timeout_ms = int(step.get("timeout_ms", 15000))

        if action == "wait":
            selector = step["selector"]
            await page.wait_for_selector(selector, timeout=timeout_ms)

        elif action == "wait_any":
            selectors: List[str] = step.get("selectors", [])
            if not selectors:
                continue
            found = None
            elapsed = 0
            tick = 250
            while elapsed < timeout_ms and not found:
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
                raise TimeoutError(f"wait_any: не дождался ни одного из: {selectors}")

        elif action == "click":
            selector = step["selector"]
            await page.wait_for_selector(selector, timeout=timeout_ms)
            await page.click(selector)

        elif action == "maybe_click":
            selector = step["selector"]
            await _maybe_click(page, selector)

        elif action == "fill":
            selector = step["selector"]
            value = step.get("value", "")
            await page.wait_for_selector(selector, timeout=timeout_ms)
            await page.fill(selector, value)

        elif action == "maybe_fill":
            selector = step["selector"]
            value = step.get("value", "")
            try:
                loc = page.locator(selector)
                if await loc.count() > 0:
                    await loc.first().fill(value)
            except Exception:
                pass

        elif action == "press":
            selector = step["selector"]
            value = step.get("value", "")
            await page.wait_for_selector(selector, timeout=timeout_ms)
            await page.press(selector, value)

        elif action == "maybe_press":
            selector = step["selector"]
            value = step.get("value", "")
            try:
                loc = page.locator(selector)
                if await loc.count() > 0:
                    await loc.first().press(value)
            except Exception:
                pass

        elif action == "sleep":
            await page.wait_for_timeout(int(step.get("value", 500)))

        else:
            raise ValueError(f"Unknown action: {action}")


def _is_on(label: str, on_hints: List[str], off_hints: List[str]) -> bool:
    lower = label.lower()
    if any(h in lower for h in on_hints):
        return True
    if any(h in lower for h in off_hints):
        return False
    return True  # неизвестно — считаем, что нужно выключить на всякий случай


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

            # Вторичная проверка — если после клика состояние не сменилось по подсказке, попробуем ещё раз.
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
    """Пытаемся гарантировать, что микрофон и камера выключены после входа."""

    await _ensure_control_off(page, MIC_SELECTORS, MIC_ON_HINTS, MIC_OFF_HINTS)
    await _ensure_control_off(page, CAMERA_SELECTORS, CAMERA_ON_HINTS, CAMERA_OFF_HINTS)
