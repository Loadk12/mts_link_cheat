from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, Dict

from .join_flow import DEFAULT_JOIN_TIMEOUTS, ensure_media_disabled
from .settings import get_logs_dir


TELEMOST_TIMEOUTS = {
    "landing_ms": 120000,
    "continue_browser_ms": 120000,
    "prejoin_ms": 120000,
    "name_ms": 60000,
    "join_button_ms": 120000,
    "after_join_ms": 180000,
    "flow_ms": 360000,
    "state_poll_ms": 2000,
}


STATE_UPDATE_MODAL = "update_modal"
STATE_DISCOVERY_MODAL = "discovery_modal"
STATE_PROFILE_MODAL = "profile_modal"
STATE_CALL_LANDING = "call_landing"
STATE_MEDIA_ERROR_VIDEO = "media_error_video"
STATE_MEDIA_ERROR_MICROPHONE = "media_error_microphone"
STATE_PREJOIN = "prejoin"
STATE_LOADING = "joining/loading"
STATE_JOINED = "joined"
STATE_UNKNOWN = "unknown"

OVERLAY_STATES = {
    STATE_UPDATE_MODAL,
    STATE_DISCOVERY_MODAL,
    STATE_PROFILE_MODAL,
    STATE_MEDIA_ERROR_VIDEO,
    STATE_MEDIA_ERROR_MICROPHONE,
}


TELEMOST_STATE_JS = r"""
() => {
  const contexts = [];
  const addContext = (win, name) => {
    try {
      const doc = win.document;
      if (!doc) return;
      contexts.push({ win, doc, name });
      for (let i = 0; i < win.frames.length; i += 1) {
        addContext(win.frames[i], `${name}.frame${i}`);
      }
    } catch (e) {}
  };
  addContext(window, "top");

  const visible = (el) => {
    if (!el) return false;
    const win = el.ownerDocument && el.ownerDocument.defaultView || window;
    const style = win.getComputedStyle(el);
    if (style.visibility === "hidden" || style.display === "none" || style.opacity === "0") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 1 && rect.height > 1;
  };
  const textOf = (el) => (el && (el.innerText || el.textContent || "") || "").replace(/\s+/g, " ").trim();
  const attrsOf = (el) => [
    textOf(el),
    el.getAttribute("aria-label"),
    el.getAttribute("label"),
    el.getAttribute("title"),
    el.getAttribute("data-testid"),
    el.getAttribute("data-test-id"),
    el.getAttribute("data-test-tag"),
    el.getAttribute("href"),
    el.getAttribute("class"),
  ].filter(Boolean).join(" ").replace(/\s+/g, " ").trim();
  const query = (selector) => {
    const out = [];
    for (const ctx of contexts) {
      try {
        for (const el of Array.from(ctx.doc.querySelectorAll(selector))) {
          out.push({ el, frame: ctx.name });
        }
      } catch (e) {}
    }
    return out;
  };

  const bodyTextRaw = contexts.map((ctx) => {
    try {
      return ctx.doc.body && ctx.doc.body.innerText || "";
    } catch (e) {
      return "";
    }
  }).filter(Boolean).join("\n");
  const bodyText = bodyTextRaw.replace(/\s+/g, " ").trim();
  const lowered = bodyText.toLowerCase();
  const controls = query("button, [role='button'], a, [aria-label], [data-testid], [data-test-tag]")
    .filter(({ el }) => visible(el));
  const buttons = controls
    .map(({ el, frame }) => ({
      frame,
      text: textOf(el),
      aria: el.getAttribute("aria-label") || "",
      title: el.getAttribute("title") || "",
      testid: el.getAttribute("data-testid") || el.getAttribute("data-test-id") || "",
      testtag: el.getAttribute("data-test-tag") || "",
      href: el.getAttribute("href") || "",
      disabled: !!el.disabled || el.getAttribute("aria-disabled") === "true",
      className: typeof el.className === "string" ? el.className : "",
    }));
  const inputs = query("input, textarea, [contenteditable='true']")
    .filter(({ el }) => visible(el))
    .map(({ el, frame }) => ({
      frame,
      type: el.getAttribute("type") || "",
      id: el.id || "",
      name: el.getAttribute("name") || "",
      placeholder: el.getAttribute("placeholder") || "",
      aria: el.getAttribute("aria-label") || "",
      value: el.value || el.textContent || "",
    }));
  const hasButton = (re) => controls.some(({ el }) => re.test(attrsOf(el)));
  const buttonText = controls.map(({ el }) => attrsOf(el)).join(" \n ");
  const hasText = (re) => re.test(bodyText) || re.test(buttonText);
  const visibleSelector = (selector) => query(selector).some(({ el }) => visible(el));

  const continueBrowser = hasButton(/meeting-continue-in-browser-continue|продолжить\s+в\s+браузере|continue\s+in\s+browser/i)
    || visibleSelector("[data-test-tag*='meeting-continue-in-browser'], [data-testid*='continue-in-browser']");
  const connectButton = hasButton(/enter-conference|(^|\s)подключиться($|\s)|join\s+meeting/i);
  const updateModal = hasText(/большое\s+обновление\s+в\s+телемосте|звучит\s+отлично/i);
  const discoveryModal = hasText(/разрешите\s+находить\s+вас|не\s+сейчас/i) && hasText(/находить\s+вас/i);
  const profileModal = hasText(/проверьте\s+профиль|всё\s+хорошо|все\s+хорошо/i) && hasText(/профиль/i);
  const videoError = hasText(/включить\s+видео\s+не\s+удалось/i);
  const microphoneError = hasText(/включить\s+микрофон\s+не\s+удалось/i);
  const callLanding = continueBrowser || /подключение\s+к\s+звонку|вы\s+подключаетесь\s+к\s+видеовстрече/i.test(bodyText);
  const prejoinNameLabel = /ваше\s+имя\s+на\s+встрече/i.test(bodyText);
  const guestDisplayName = /ваше\s+имя\s+на\s+встрече\s+гость/i.test(bodyText)
    || (connectButton && /(^|\s)гость($|\s)/i.test(bodyText));
  const prejoinMediaControls = (connectButton || prejoinNameLabel)
    && hasButton(/(включить|выключить)\s+(микрофон|камеру|камерa|видео)|microphone|camera/i);
  const prejoin = !callLanding && (connectButton || prejoinNameLabel || guestDisplayName || prejoinMediaControls);
  const connectingStatus = /групповой\s+звонок\s+подключение/i.test(bodyText)
    || (!callLanding && !prejoin && /(^|\s)подключение($|\s)/i.test(bodyText));
  const leaveButton = hasButton(/end-call|hangup|hang-up|leave|выйти\s+из\s+встречи|покинуть|завершить/i);
  const participantsButton = hasButton(/participants-button|участники/i);
  const chatButton = hasButton(/chat-alt-button|открыть\s+чат|(^|\s)чат($|\s)/i);
  const demonstrationButton = hasButton(/start-demonstration|демонстрация|screen-sharing|screensharing/i);
  const moreButton = hasButton(/more-popup|ещё|еще|more/i);
  const meetingToolbar = participantsButton || chatButton || demonstrationButton || moreButton;
  const blockingOverlay = updateModal || discoveryModal || profileModal || videoError || microphoneError;
  const joined = !blockingOverlay && !callLanding && !prejoin && !connectingStatus && leaveButton && meetingToolbar;
  const loading = /загрузка|loading|connecting|подождите|ожидание|инициализац/i.test(lowered)
    || connectingStatus
    || query("[aria-busy='true'], [role='progressbar'], .spinner, [class*='spin'], [class*='loader']")
      .some(({ el }) => visible(el));

  const prejoinSignals = [];
  if (connectButton) prejoinSignals.push("connect_button");
  if (prejoinNameLabel) prejoinSignals.push("name_label");
  if (guestDisplayName) prejoinSignals.push("guest_display_name");
  if (prejoinMediaControls) prejoinSignals.push("prejoin_media_controls");
  if (continueBrowser) prejoinSignals.push("continue_browser");
  if (blockingOverlay) prejoinSignals.push("blocking_overlay");
  if (connectingStatus) prejoinSignals.push("connecting_status");

  const joinedSignals = [];
  if (leaveButton) joinedSignals.push("leave_button");
  if (participantsButton) joinedSignals.push("participants_button");
  if (chatButton) joinedSignals.push("chat_button");
  if (demonstrationButton) joinedSignals.push("demonstration_button");
  if (moreButton) joinedSignals.push("more_button");

  let state = "unknown";
  if (updateModal) state = "update_modal";
  else if (discoveryModal) state = "discovery_modal";
  else if (profileModal) state = "profile_modal";
  else if (videoError) state = "media_error_video";
  else if (microphoneError) state = "media_error_microphone";
  else if (callLanding) state = "call_landing";
  else if (prejoin) state = "prejoin";
  else if (loading) state = "joining/loading";
  else if (joined) state = "joined";

  return {
    state,
    url: location.href,
    frames: contexts.map((ctx) => {
      try {
        return { name: ctx.name, url: ctx.win.location.href };
      } catch (e) {
        return { name: ctx.name, url: "" };
      }
    }),
    bodyLength: bodyText.trim().length,
    bodySample: bodyText.slice(0, 500),
    buttons: buttons.slice(0, 120),
    inputs: inputs.slice(0, 40),
    flags: {
      continueBrowser,
      connectButton,
      updateModal,
      discoveryModal,
      profileModal,
      videoError,
      microphoneError,
      callLanding,
      prejoinNameLabel,
      guestDisplayName,
      prejoinMediaControls,
      connectingStatus,
      prejoin,
      leaveButton,
      participantsButton,
      chatButton,
      demonstrationButton,
      moreButton,
      meetingToolbar,
      joined,
      loading,
    },
    prejoin_signals: prejoinSignals,
    joined_signals: joinedSignals,
    conflict: prejoinSignals.length > 0 && joinedSignals.length > 0
      ? { prejoin_signals: prejoinSignals, joined_signals: joinedSignals, chosen: state }
      : null,
  };
}
"""


def _log(logger, message: str) -> None:
    if logger:
        logger.info(message)


def _timeouts(join_cfg: Dict[str, Any], overrides: Dict[str, Any] | None) -> Dict[str, int]:
    merged = dict(DEFAULT_JOIN_TIMEOUTS)
    merged.update(TELEMOST_TIMEOUTS)
    merged.update(join_cfg.get("timeouts") or {})
    merged.update(overrides or {})
    return {key: int(value) for key, value in merged.items() if value is not None}


def _locator_targets(page):
    yield page, "page"
    try:
        main_frame = page.main_frame
        frames = list(page.frames)
    except Exception:
        return
    for frame in frames:
        if frame == main_frame:
            continue
        yield frame, f"frame={frame.url}"


def get_configured_display_name(join_cfg: Dict[str, Any], *fallback_cfgs: Dict[str, Any]) -> str:
    for cfg in (join_cfg, *fallback_cfgs):
        if not isinstance(cfg, dict):
            continue
        for key in ("display_name", "join_name", "name"):
            value = cfg.get(key)
            if value:
                return str(value).strip()
        for step in cfg.get("steps") or []:
            if not isinstance(step, dict):
                continue
            action = str(step.get("action") or "").lower()
            selector = str(step.get("selector") or "").lower()
            if action not in {"fill", "maybe_fill"}:
                continue
            if selector == "#name" or "name" in selector or "имя" in selector:
                value = step.get("value")
                if value:
                    return str(value).strip()
    return ""


async def save_telemost_diagnostics(page, stage: str, logger=None) -> str:
    logs_dir = get_logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)
    safe_stage = re.sub(r"[^\w\-]+", "_", stage or "unknown")
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = logs_dir / f"telemost_{safe_stage}_{ts}"

    try:
        await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
    except Exception as e:
        _log(logger, f"telemost: diagnostic screenshot failed: {e}")

    try:
        base.with_suffix(".html").write_text(
            await page.content(),
            encoding="utf-8",
            errors="ignore",
        )
    except Exception as e:
        _log(logger, f"telemost: diagnostic html failed: {e}")

    try:
        details = await page.evaluate(
            r"""
() => {
  const contexts = [];
  const addContext = (win, name) => {
    try {
      const doc = win.document;
      if (!doc) return;
      contexts.push({ win, doc, name });
      for (let i = 0; i < win.frames.length; i += 1) {
        addContext(win.frames[i], `${name}.frame${i}`);
      }
    } catch (e) {}
  };
  addContext(window, "top");
  const norm = (value) => (value || "").replace(/\s+/g, " ").trim();
  const query = (selector) => {
    const out = [];
    for (const ctx of contexts) {
      try {
        for (const el of Array.from(ctx.doc.querySelectorAll(selector))) {
          out.push({ el, frame: ctx.name });
        }
      } catch (e) {}
    }
    return out;
  };
  const buttons = query("button, [role='button'], a").slice(0, 160).map(({ el, frame }, i) => ({
    index: i,
    frame,
    text: norm(el.innerText || el.textContent),
    ariaLabel: el.getAttribute("aria-label") || "",
    disabled: !!el.disabled || el.getAttribute("aria-disabled") === "true",
    dataTestId: el.getAttribute("data-testid") || "",
    dataTestTag: el.getAttribute("data-test-tag") || "",
    className: el.getAttribute("class") || "",
  }));
  const inputs = query("input, textarea, [contenteditable='true']").slice(0, 160).map(({ el, frame }, i) => ({
    index: i,
    frame,
    tag: el.tagName.toLowerCase(),
    type: el.getAttribute("type") || "",
    id: el.getAttribute("id") || "",
    name: el.getAttribute("name") || "",
    placeholder: el.getAttribute("placeholder") || "",
    ariaLabel: el.getAttribute("aria-label") || "",
    value: el.value || el.textContent || "",
  }));
  const frames = contexts.map((ctx) => {
    try {
      return { name: ctx.name, url: ctx.win.location.href };
    } catch (e) {
      return { name: ctx.name, url: "" };
    }
  });
  return { url: location.href, frames, buttons, inputs };
}
"""
        )
        lines = [f"url: {details.get('url') or page.url}", "", "frames:"]
        for item in details.get("frames") or []:
            lines.append(f"- {item.get('name')}: {item.get('url')}")
        lines.append("")
        lines.append("buttons:")
        for item in details.get("buttons") or []:
            lines.append(
                f"- #{item.get('index')} frame={item.get('frame')!r} text={item.get('text')!r} "
                f"aria-label={item.get('ariaLabel')!r} disabled={item.get('disabled')!r} "
                f"data-testid={item.get('dataTestId')!r} data-test-tag={item.get('dataTestTag')!r} "
                f"class={item.get('className')!r}"
            )
        lines.append("")
        lines.append("inputs:")
        for item in details.get("inputs") or []:
            lines.append(
                f"- #{item.get('index')} frame={item.get('frame')!r} tag={item.get('tag')!r} type={item.get('type')!r} "
                f"id={item.get('id')!r} name={item.get('name')!r} "
                f"placeholder={item.get('placeholder')!r} aria-label={item.get('ariaLabel')!r} "
                f"value={item.get('value')!r}"
            )
        base.with_suffix(".txt").write_text("\n".join(lines), encoding="utf-8")
    except Exception as e:
        _log(logger, f"telemost: diagnostic controls failed: {e}")

    _log(logger, f"telemost: diagnostics saved: {base}")
    return str(base)


async def detect_telemost_state(page) -> Dict[str, Any]:
    try:
        res = await page.evaluate(TELEMOST_STATE_JS)
        if isinstance(res, dict):
            return res
    except Exception as e:
        return {"state": "closed", "url": page.url, "error": str(e)}
    return {"state": STATE_UNKNOWN, "url": page.url}


def _log_state_conflict(info: Dict[str, Any], logger=None) -> None:
    conflict = info.get("conflict")
    if not conflict:
        return
    _log(
        logger,
        "telemost state conflict: "
        f"prejoin_signals={conflict.get('prejoin_signals') or []} "
        f"joined_signals={conflict.get('joined_signals') or []} "
        f"chosen={conflict.get('chosen') or info.get('state')}",
    )


async def assert_real_telemost_joined(page, logger=None, log_failures: bool = True) -> bool:
    info = await detect_telemost_state(page)
    _log_state_conflict(info, logger)
    state = str(info.get("state") or STATE_UNKNOWN)
    if state == STATE_JOINED:
        _log(logger, "telemost joined sanity check passed")
        return True
    if log_failures:
        _log(
            logger,
            "telemost joined sanity check failed: "
            f"state={state} prejoin_signals={info.get('prejoin_signals') or []} "
            f"joined_signals={info.get('joined_signals') or []}",
        )
    return False


async def _first_visible(page, selector: str):
    for target, _target_name in _locator_targets(page):
        loc = target.locator(selector)
        try:
            count = await loc.count()
        except Exception:
            continue
        for index in range(min(count, 12)):
            candidate = loc.nth(index)
            try:
                if await candidate.is_visible():
                    return candidate
            except Exception:
                continue
    return None


async def _find_continue_button(page):
    selectors = [
        "button[data-testid='meeting-continue-in-browser-continue']:not([disabled])",
        "[role='button'][data-testid='meeting-continue-in-browser-continue']",
        "[data-test-tag='meeting-continue-in-browser'] button:has-text(\"Продолжить в браузере\")",
        "button[class*='continueInBrowserButton']",
        "[role='button'][class*='continueInBrowserButton']",
        "button:has-text(\"Продолжить в браузере\")",
        "[role='button']:has-text(\"Продолжить в браузере\")",
    ]
    for selector in selectors:
        button = await _first_visible(page, selector)
        if button:
            return button, selector

    try:
        role = page.get_by_role(
            "button",
            name=re.compile(r"продолжить\s+в\s+браузере", re.I),
        )
        if await role.count() > 0 and await role.first.is_visible():
            return role.first, "role=button name=/продолжить в браузере/i"
    except Exception:
        pass
    return None, ""


async def _find_connect_button(page):
    selectors = [
        "button[data-testid='enter-conference-button']:not([disabled])",
        "button[data-testid*='enter-conference']:not([disabled])",
        "button[class*='joinMeetingButton']:not([disabled])",
        "button:has-text(\"Подключиться\")",
        "[role='button']:has-text(\"Подключиться\")",
    ]
    for selector in selectors:
        try:
            button = await _first_visible(page, selector)
            if button and await button.is_enabled():
                return button, selector
        except Exception:
            continue

    for target, target_name in _locator_targets(page):
        try:
            role = target.get_by_role("button", name=re.compile(r"подключиться", re.I))
            if await role.count() > 0 and await role.first.is_visible() and await role.first.is_enabled():
                return role.first, f"{target_name} role=button name=/подключиться/i"
        except Exception:
            pass
    return None, ""


async def _click_locator(locator) -> bool:
    try:
        await locator.scroll_into_view_if_needed(timeout=1000)
    except Exception:
        pass
    try:
        await locator.click(timeout=3000)
        return True
    except Exception:
        try:
            await locator.evaluate("el => el.click()")
            return True
        except Exception:
            return False


async def _find_button_by_patterns(page, patterns, selectors=()):
    for selector in selectors:
        button = await _first_visible(page, selector)
        if button:
            return button, selector

    for target, target_name in _locator_targets(page):
        for pattern in patterns:
            try:
                role = target.get_by_role("button", name=pattern)
                if await role.count() > 0 and await role.first.is_visible():
                    return role.first, f"{target_name} role=button name={pattern.pattern!r}"
            except Exception:
                pass

    for target, target_name in _locator_targets(page):
        for pattern in patterns:
            try:
                loc = target.locator("button, [role='button']").filter(has_text=pattern)
                count = await loc.count()
            except Exception:
                continue
            for index in range(min(count, 8)):
                button = loc.nth(index)
                try:
                    if await button.is_visible():
                        return button, f"{target_name} button has_text={pattern.pattern!r}"
                except Exception:
                    continue

    return None, ""


async def _dismiss_telemost_state(page, state: str, logger=None) -> str:
    actions = {
        STATE_UPDATE_MODAL: (
            "update_modal",
            [re.compile(r"звучит\s+отлично", re.I)],
            ("button:has-text(\"Звучит отлично\")", "[role='button']:has-text(\"Звучит отлично\")"),
        ),
        STATE_DISCOVERY_MODAL: (
            "discovery_modal",
            [re.compile(r"не\s+сейчас", re.I)],
            ("button:has-text(\"Не сейчас\")", "[role='button']:has-text(\"Не сейчас\")"),
        ),
        STATE_PROFILE_MODAL: (
            "profile_modal",
            [re.compile(r"вс[её]\s+хорошо", re.I)],
            ("button:has-text(\"Всё хорошо\")", "button:has-text(\"Все хорошо\")"),
        ),
        STATE_MEDIA_ERROR_VIDEO: (
            "media_error_video",
            [re.compile(r"понятно", re.I)],
            ("button:has-text(\"Понятно\")", "[role='button']:has-text(\"Понятно\")"),
        ),
        STATE_MEDIA_ERROR_MICROPHONE: (
            "media_error_microphone",
            [re.compile(r"понятно", re.I)],
            ("button:has-text(\"Понятно\")", "[role='button']:has-text(\"Понятно\")"),
        ),
    }
    action = actions.get(state)
    if not action:
        return ""

    name, patterns, selectors = action
    button, selector = await _find_button_by_patterns(page, patterns, selectors)
    if not button and state in {STATE_UPDATE_MODAL, STATE_PROFILE_MODAL}:
        button, selector = await _first_visible(page, "button[aria-label='Закрыть']"), "button[aria-label='Закрыть']"
    if not button:
        return ""

    if await _click_locator(button):
        _log(logger, f"telemost overlay dismissed={name} via {selector}")
        await page.wait_for_timeout(600)
        return name
    return ""


async def dismiss_known_telemost_overlays(page, logger=None) -> list[str]:
    dismissed: list[str] = []
    for _ in range(4):
        info = await detect_telemost_state(page)
        state = str(info.get("state") or STATE_UNKNOWN)
        if state not in OVERLAY_STATES:
            break
        name = await _dismiss_telemost_state(page, state, logger)
        if not name:
            break
        dismissed.append(name)
    return dismissed


async def _find_name_control(page):
    selectors = [
        "input[data-testid='orb-textinput-input']",
        "input[aria-label*='имя' i]",
        "input[placeholder*='имя' i]",
        "input[name*='name' i]",
        "input[placeholder*='name' i]",
        "input[type='text']",
        "input:not([type])",
        "textarea[aria-label*='имя' i]",
        "[contenteditable='true']",
    ]
    for selector in selectors:
        control = await _first_visible(page, selector)
        if control:
            return control, selector
    return None, ""


async def _open_name_editor_if_needed(page) -> None:
    for text in ("Гость", "Ваше имя на встрече"):
        for target, _target_name in _locator_targets(page):
            try:
                loc = target.get_by_text(re.compile(text, re.I)).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=1000)
                    await page.wait_for_timeout(300)
                    return
            except Exception:
                continue


async def _control_value(control) -> str:
    try:
        return await control.evaluate(
            """el => {
              if ('value' in el) return el.value || '';
              return el.innerText || el.textContent || '';
            }"""
        )
    except Exception:
        return ""


async def read_telemost_prejoin_display_name(page) -> str:
    try:
        value = await page.evaluate(
            r"""
() => {
  const texts = [];
  const addContext = (win) => {
    try {
      const doc = win.document;
      if (doc && doc.body) texts.push(doc.body.innerText || "");
      for (let i = 0; i < win.frames.length; i += 1) addContext(win.frames[i]);
    } catch (e) {}
  };
  addContext(window);
  const raw = texts.join("\n");
  const lines = raw.split(/\n+/).map((line) => line.trim()).filter(Boolean);
  for (let i = 0; i < lines.length; i += 1) {
    if (/^ваше\s+имя\s+на\s+встрече$/i.test(lines[i]) && lines[i + 1]) {
      return lines[i + 1];
    }
  }
  const compact = raw.replace(/\s+/g, " ").trim();
  const match = compact.match(/ваше\s+имя\s+на\s+встрече\s+(.+?)\s+(подключиться|включить|выключить|настройки|$)/i);
  return match ? match[1].trim() : "";
}
"""
        )
        return str(value or "").strip()
    except Exception:
        return ""


async def _fill_text_control(control, value: str) -> None:
    tag = await control.evaluate("el => el.tagName.toLowerCase()")
    if tag in {"input", "textarea"}:
        await control.fill(value, timeout=5000)
        await control.evaluate(
            """(el, val) => {
              const proto = el.tagName.toLowerCase() === 'textarea'
                ? HTMLTextAreaElement.prototype
                : HTMLInputElement.prototype;
              const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
              el.focus();
              setter.call(el, val);
              el.dispatchEvent(new Event('input', { bubbles: true }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            value,
        )
    else:
        await control.fill(value, timeout=5000)
        await control.evaluate(
            """(el, val) => {
              el.textContent = val;
              el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: val }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            value,
        )


async def set_telemost_display_name(page, configured_name: str, timeout_ms: int, logger=None) -> bool:
    configured_name = (configured_name or "").strip()
    if not configured_name:
        _log(logger, "telemost: no configured display name found, keeping current name")
        return False

    prejoin_name = await read_telemost_prejoin_display_name(page)
    if prejoin_name:
        _log(logger, f"telemost prejoin display_name={prejoin_name}")

    deadline = time.monotonic() * 1000 + timeout_ms
    control = None
    selector = ""
    while time.monotonic() * 1000 < deadline:
        control, selector = await _find_name_control(page)
        if control:
            break
        await _open_name_editor_if_needed(page)
        await page.wait_for_timeout(500)

    if not control:
        if prejoin_name == configured_name:
            _log(logger, "telemost: display name updated")
            return True
        _log(
            logger,
            f"telemost: display_name={configured_name} not editable in current profile; keeping page profile name",
        )
        return False

    current_name = (await _control_value(control)).strip()
    _log(logger, f"telemost: current display name={current_name}")
    _log(logger, f"telemost: display_name={current_name}")
    if current_name == configured_name:
        _log(logger, "telemost: display name updated")
        return True

    _log(logger, f"telemost: setting display name={configured_name}")
    try:
        await control.click(timeout=5000)
        await _fill_text_control(control, configured_name)
    except Exception as e:
        _log(
            logger,
            f"telemost: display_name={configured_name} not editable via {selector}; keeping page profile name: {e}",
        )
        return False
    try:
        await control.evaluate("el => el.blur()")
    except Exception:
        try:
            await control.press("Tab", timeout=1000)
        except Exception:
            pass

    verify_deadline = min(deadline, time.monotonic() * 1000 + 5000)
    while time.monotonic() * 1000 < verify_deadline:
        updated = (await _control_value(control)).strip()
        if updated == configured_name:
            _log(logger, "telemost: display name updated")
            return True
        await page.wait_for_timeout(250)

    _log(
        logger,
        f"telemost: display_name={configured_name} was not accepted via {selector}; keeping page profile name",
    )
    return False


async def perform_telemost_prejoin(
    page,
    join_cfg: Dict[str, Any],
    logger=None,
    timeouts: Dict[str, Any] | None = None,
) -> bool:
    telemost_timeouts = _timeouts(join_cfg, timeouts)
    poll_ms = int(telemost_timeouts.get("state_poll_ms", 2000))
    flow_ms = int(telemost_timeouts.get("flow_ms", 360000))
    join_button_timeout = int(telemost_timeouts.get("join_button_ms", 120000))
    name_timeout = int(telemost_timeouts.get("name_ms", telemost_timeouts.get("name_input_ms", 60000)))
    started_at = time.monotonic()
    deadline = started_at * 1000 + flow_ms

    _log(logger, "telemost: browser-only mode / external app handling")
    display_name = get_configured_display_name(join_cfg)
    last_state = ""
    last_continue_at = 0.0
    last_connect_at = 0.0
    prejoin_logged = False
    name_attempted = False
    waiting_logged = False

    while time.monotonic() * 1000 < deadline:
        await dismiss_known_telemost_overlays(page, logger)
        state_info = await detect_telemost_state(page)
        _log_state_conflict(state_info, logger)
        state = str(state_info.get("state") or STATE_UNKNOWN)
        if state != last_state:
            _log(logger, f"telemost state={state} url={state_info.get('url', page.url)}")
            last_state = state

        if state == "closed":
            raise TimeoutError(f"telemost: page closed during prejoin: {state_info.get('error', '')}")

        if state == STATE_JOINED:
            if await assert_real_telemost_joined(page, logger):
                elapsed = int(time.monotonic() - started_at)
                _log(logger, f"telemost: joined detected after {elapsed} sec")
                return True
            await page.wait_for_timeout(min(poll_ms, 1500))
            continue

        if state in OVERLAY_STATES:
            dismissed = await _dismiss_telemost_state(page, state, logger)
            if dismissed:
                continue

        if state == STATE_CALL_LANDING:
            now = time.monotonic()
            if now - last_continue_at >= 1.5:
                _log(logger, "telemost: Continue in browser screen detected")
                button, selector = await _find_continue_button(page)
                if button:
                    _log(logger, "telemost clicking continue_in_browser")
                    try:
                        await button.evaluate("el => el.click()")
                    except Exception:
                        await _click_locator(button)
                    _log(logger, f"telemost: Continue in browser clicked via {selector}")
                    last_continue_at = now
                else:
                    _log(logger, "telemost: Continue in browser button visible in state but selector not found")
            await page.wait_for_timeout(min(poll_ms, 1500))
            continue

        if state == STATE_PREJOIN:
            if not prejoin_logged:
                _log(logger, "telemost prejoin detected")
                prejoin_logged = True

            if not name_attempted:
                await set_telemost_display_name(page, display_name, name_timeout, logger)
                name_attempted = True

            try:
                await ensure_media_disabled(page)
            except Exception as e:
                _log(logger, f"telemost: prejoin media check skipped: {e}")

            now = time.monotonic()
            if now - last_connect_at >= 5:
                button, selector = await _find_connect_button(page)
                if button:
                    _log(logger, "telemost clicking connect")
                    await button.click(timeout=join_button_timeout)
                    _log(logger, f"telemost: Connect clicked via {selector}")
                    if not waiting_logged:
                        _log(logger, "telemost: waiting for real meeting UI")
                        waiting_logged = True
                    last_connect_at = now
                else:
                    _log(logger, "telemost: prejoin detected but Connect button not found")
            await page.wait_for_timeout(min(poll_ms, 1500))
            continue

        remaining_ms = max(0, int(deadline - time.monotonic() * 1000))
        await page.wait_for_timeout(min(poll_ms, remaining_ms))

    await save_telemost_diagnostics(page, "prejoin_state_machine_timeout", logger)
    return False
