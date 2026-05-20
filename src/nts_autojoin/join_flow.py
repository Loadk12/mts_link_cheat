import re
import time
from typing import Any, Dict, List

from .healthcheck import detect_join_state, is_joined_to_meeting


DEFAULT_JOIN_TIMEOUTS = {
    "landing_ms": 120000,
    "name_input_ms": 120000,
    "join_button_ms": 120000,
    "post_join_grace_ms": 15000,
    "after_join_ms": 180000,
    "in_meeting_ms": 180000,
    "final_join_after_click_ms": 180000,
    "state_poll_ms": 2000,
}


FINAL_JOIN_BUTTON_SELECTORS = [
    "button[data-testid*=\"join\" i]",
    "button[aria-label*=\"Присоединиться к встрече\" i]",
    "[role=\"button\"][aria-label*=\"Присоединиться к встрече\" i]",
    "button:has-text(\"Присоединиться к встрече\")",
    "button:has-text(\"ПРИСОЕДИНИТЬСЯ К ВСТРЕЧЕ\")",
    "[role=\"button\"]:has-text(\"Присоединиться к встрече\")",
    "[role=\"button\"]:has-text(\"ПРИСОЕДИНИТЬСЯ К ВСТРЕЧЕ\")",
]


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


async def _click_final_join_button(page, timeout_ms: int) -> str:
    deadline = time.monotonic() * 1000 + timeout_ms
    poll_ms = 250
    role_name = re.compile(r"присоединиться\s+к\s+встрече", re.I)

    while time.monotonic() * 1000 < deadline:
        for selector in FINAL_JOIN_BUTTON_SELECTORS:
            try:
                loc = page.locator(selector)
                if await loc.count() > 0:
                    button = loc.first
                    if await button.is_enabled(timeout=500):
                        await button.click(timeout=1000)
                        return selector
            except Exception:
                pass
        try:
            role_loc = page.get_by_role("button", name=role_name)
            if await role_loc.count() > 0:
                button = role_loc.first
                if await button.is_enabled(timeout=500):
                    await button.click(timeout=1000)
                    return "role=button name=/присоединиться к встрече/i"
        except Exception:
            pass
        await page.wait_for_timeout(poll_ms)

    raise TimeoutError("final join button not found or not clickable")


def _timeouts(join_cfg: Dict[str, Any], overrides: Dict[str, Any] | None) -> Dict[str, int]:
    merged = dict(DEFAULT_JOIN_TIMEOUTS)
    merged.update(join_cfg.get("timeouts") or {})
    merged.update(overrides or {})
    return {k: int(v) for k, v in merged.items() if v is not None}


def _log(logger, message: str):
    if logger:
        logger.info(message)


def _step_label(step: Dict[str, Any]) -> str:
    selector = step.get("selector") or ",".join(step.get("selectors") or [])
    return f"{step.get('action')} {selector}".strip()


def _looks_like_join_selector(selector: str) -> bool:
    lower = (selector or "").lower()
    return any(
        hint in lower
        for hint in (
            "enter-to-event",
            "join",
            "присоедин",
            "войти",
            "eventlanding",
        )
    )


async def _finish_if_joined(page, logger, label: str) -> bool:
    if await is_joined_to_meeting(page):
        _log(logger, f"detected in-meeting UI after {label}; join success")
        return True
    return False


async def wait_post_join_transition(page, timeouts: Dict[str, int], logger=None) -> bool:
    grace_ms = int(timeouts.get("post_join_grace_ms", 15000))
    after_join_ms = int(timeouts.get("after_join_ms", 180000))
    in_meeting_ms = int(timeouts.get("in_meeting_ms", after_join_ms))
    poll_ms = int(timeouts.get("state_poll_ms", 2000))
    deadline_ms = time.monotonic() * 1000 + max(after_join_ms, in_meeting_ms)
    grace_deadline_ms = time.monotonic() * 1000 + grace_ms
    start = time.monotonic()
    final_join_clicked = False

    _log(logger, f"post-join grace started {grace_ms}ms")
    _log(logger, f"waiting in-meeting UI up to {max(after_join_ms, in_meeting_ms)}ms")

    while time.monotonic() * 1000 < deadline_ms:
        if await is_joined_to_meeting(page):
            elapsed = int(time.monotonic() - start)
            _log(logger, f"joined detected after {elapsed}s")
            return True

        state_info = await detect_join_state(page)
        state = state_info.get("state", "unknown")
        elapsed = int(time.monotonic() - start)
        _log(
            logger,
            "post join state: "
            f"{state} elapsed={elapsed}s url={state_info.get('url', '')} "
            f"buttons={state_info.get('buttons', '-')}",
        )

        if state == "joined":
            _log(logger, f"joined detected after {elapsed}s")
            return True
        if state == "device_prejoin" and not final_join_clicked:
            _log(logger, "device prejoin screen detected")
            try:
                await ensure_media_disabled(page)
            except Exception as e:
                _log(logger, f"device prejoin media toggle skipped: {e}")
            _log(logger, "final join button detected")
            _log(logger, "clicking final join button")
            clicked_selector = await _click_final_join_button(page, int(timeouts.get("join_button_ms", 120000)))
            final_join_clicked = True
            deadline_ms = max(
                deadline_ms,
                time.monotonic() * 1000 + int(timeouts.get("final_join_after_click_ms", after_join_ms)),
            )
            _log(logger, f"final join clicked: {clicked_selector}")
            _log(logger, "waiting real in-meeting UI")
            await page.wait_for_timeout(poll_ms)
            continue
        if state == "closed":
            raise TimeoutError(f"browser/page closed during post-join wait: {state_info.get('error', '')}")
        if state == "explicit_error" and time.monotonic() * 1000 > grace_deadline_ms:
            raise TimeoutError(f"explicit join error on page: {state_info.get('sample', '')}")

        if (
            state in ("landing", "prejoin")
            and state_info.get("joinButton")
            and not state_info.get("finalJoinButton")
            and time.monotonic() * 1000 > grace_deadline_ms
        ):
            _log(logger, "post-join wait found another join/prejoin button; continuing configured join steps")
            return False

        await page.wait_for_timeout(poll_ms)

    if await is_joined_to_meeting(page):
        elapsed = int(time.monotonic() - start)
        _log(logger, f"joined detected after {elapsed}s at timeout boundary")
        return True
    elapsed = int(time.monotonic() - start)
    _log(logger, f"post-join timeout after {elapsed}s")
    return False


async def perform_join(page, join_cfg: Dict[str, Any], logger=None, timeouts: Dict[str, Any] | None = None):
    """
    Execute configured join steps. If the webinar UI is detected at any point,
    stop immediately and report success instead of waiting for pre-join controls.
    """
    _log(logger, "opening landing page / starting join flow")
    join_timeouts = _timeouts(join_cfg, timeouts)
    if await _finish_if_joined(page, logger, "initial page"):
        return True

    steps = join_cfg.get("steps", [])
    for step in steps:
        action = step.get("action")
        timeout_ms = int(step.get("timeout_ms", 15000))
        if action == "wait" and step.get("selector") == "#EventEnterForm":
            timeout_ms = max(timeout_ms, join_timeouts.get("landing_ms", timeout_ms))
        if action in ("click", "click_any", "wait_any"):
            timeout_ms = max(timeout_ms, join_timeouts.get("join_button_ms", timeout_ms))
        if action in ("fill", "maybe_fill") and step.get("selector") == "#name":
            timeout_ms = max(timeout_ms, join_timeouts.get("name_input_ms", timeout_ms))
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
            if _looks_like_join_selector(selector):
                _log(logger, "clicked join button")
                if await wait_post_join_transition(page, join_timeouts, logger=logger):
                    return True

        elif action == "click_any":
            selectors: List[str] = step.get("selectors", [])
            if not selectors:
                continue
            _log(logger, f"clicking first available selector: {selectors}")
            clicked_selector = await _click_any(page, selectors, timeout_ms)
            _log(logger, f"clicked join button: {clicked_selector}")
            if await wait_post_join_transition(page, join_timeouts, logger=logger):
                return True

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
