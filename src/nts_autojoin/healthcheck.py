import json


JOINED_JS = r"""
() => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
  };
  const norm = (value) => (value || "").toLowerCase();
  const textOf = (el) => norm([
    el.getAttribute("aria-label"),
    el.getAttribute("label"),
    el.getAttribute("title"),
    el.getAttribute("data-testid"),
    el.innerText,
    el.textContent,
  ].filter(Boolean).join(" "));

  const buttons = Array.from(document.querySelectorAll("button, [role='button'], [aria-label], [data-testid]"))
    .filter(visible);
  const labels = buttons.map(textOf);

  const leaveHints = [
    "покинуть", "выйти", "завершить", "leave", "hang up", "end call",
  ];
  const finalJoinHints = [
    "присоединиться к встрече", "join meeting",
  ];
  const controlHints = [
    "microphone", "camera", "video", "screensharing", "screen sharing",
    "микроф", "камер", "демонстрац", "экран", "поднять руку", "raise_hand", "raise hand",
  ];

  const finalJoinButton = labels.some(label => finalJoinHints.some(h => label.includes(h)));
  const leaveButton = labels.some(label => leaveHints.some(h => label.includes(h)));
  const controlCount = labels.filter(label => controlHints.some(h => label.includes(h))).length;
  const chatInput = !!Array.from(document.querySelectorAll("[contenteditable='true'], textarea, input"))
    .find(el => visible(el) && /сообщ|message|chat|введите/.test(textOf(el)));
  const mediaLayout = !!Array.from(document.querySelectorAll("video, canvas, [data-testid*='Layout' i], [data-testid*='Webinar' i], [class*='webinar' i], [class*='conference' i], [class*='meeting' i]"))
    .find(visible);
  const bodyText = norm(document.body ? document.body.innerText : "");
  const deviceControl = /камера|микрофон|проверить звук|camera|microphone|audio input/.test(bodyText)
    || labels.some(label => /камера|микрофон|camera|microphone/.test(label));
  const devicePreview = !!Array.from(document.querySelectorAll("video, canvas, [class*='preview' i], [class*='avatar' i]")).find(visible);
  const urlLooksInside = /\/(event|webinar|meeting|room|session|call)\b/i.test(location.pathname)
    && !/landing|enter|login/i.test(location.pathname);

  if (finalJoinButton || (deviceControl && devicePreview && !leaveButton)) {
    return { joined: false, finalJoinButton, deviceControl, devicePreview, leaveButton, controlCount, chatInput, mediaLayout, url: location.href };
  }

  const joined = leaveButton || (mediaLayout && (chatInput || controlCount >= 1)) || (urlLooksInside && leaveButton);
  return { joined, finalJoinButton, deviceControl, devicePreview, leaveButton, controlCount, chatInput, mediaLayout, url: location.href };
}
"""


JOIN_STATE_JS = r"""
() => {
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
  };
  const norm = (value) => (value || "").toLowerCase();
  const textOf = (el) => norm([
    el.getAttribute("aria-label"),
    el.getAttribute("label"),
    el.getAttribute("title"),
    el.getAttribute("data-testid"),
    el.getAttribute("placeholder"),
    el.innerText,
    el.textContent,
  ].filter(Boolean).join(" "));

  const bodyText = norm(document.body ? document.body.innerText : "");
  const bodyLength = bodyText.trim().length;
  const url = location.href;
  const buttons = Array.from(document.querySelectorAll("button, [role='button']"))
    .filter(visible);
  const labels = buttons.map(textOf);

  const joinHints = ["присоедин", "войти", "join", "enter"];
  const finalJoinHints = ["присоединиться к встрече", "join meeting"];
  const joinButton = labels.some(label => joinHints.some(h => label.includes(h)));
  const finalJoinButton = labels.some(label => finalJoinHints.some(h => label.includes(h)));
  const nameInput = !!document.querySelector("#name, input[name*='name' i], input[placeholder*='имя' i], input[placeholder*='name' i]");
  const landingForm = !!document.querySelector("#EventEnterForm") || (nameInput && joinButton);
  const deviceControl = /камера|микрофон|проверить звук|camera|microphone|audio input/.test(bodyText)
    || labels.some(label => /камера|микрофон|camera|microphone/.test(label));
  const devicePreview = !!Array.from(document.querySelectorAll("video, canvas, [class*='preview' i], [class*='avatar' i]")).find(visible);
  const spinner = !!Array.from(document.querySelectorAll(
    "[role='progressbar'], [aria-busy='true'], .spinner, [class*='spinner' i], [class*='loader' i], [class*='loading' i], [data-testid*='loader' i], [data-testid*='loading' i]"
  )).find(visible);
  const loadingText = /загруз|подожд|loading|connecting|подключ|инициализац/.test(bodyText);
  const rootOnly = !!document.querySelector("#root, #app, [data-reactroot]") && buttons.length === 0 && bodyLength < 80;

  const explicitError = /ошибка|не удалось|нет доступа|доступ запрещ|встреча заверш|мероприятие заверш|error|failed|denied|not found/.test(bodyText)
    && !loadingText;

  let state = "unknown";
  if (finalJoinButton || (deviceControl && devicePreview && joinButton)) state = "device_prejoin";
  else if (landingForm && joinButton) state = nameInput ? "landing" : "prejoin";
  else if (spinner || loadingText || rootOnly || bodyLength < 20) state = "loading";
  else if (explicitError) state = "explicit_error";

  return {
    state,
    url,
    bodyLength,
    buttons: buttons.length,
    joinButton,
    finalJoinButton,
    nameInput,
    deviceControl,
    devicePreview,
    spinner,
    loadingText,
    rootOnly,
    sample: bodyText.slice(0, 240),
  };
}
"""


HEALTH_JS = r"""
() => new Promise(async (resolve) => {
  try {
    const result = { selector_ok: false, ws_open: false, webrtc_active: false };
    try {
      const selList = __INDICATOR_SELECTORS__;
      if (selList && selList.length > 0) {
        for (const sel of selList) {
          try {
            if (sel && document.querySelector(sel)) { result.selector_ok = true; break; }
          } catch(e){}
        }
      }
    } catch (e) {}
    try {
      const entries = performance.getEntriesByType('resource') || [];
      const hasWS = entries.some(e => (e.name||'').startsWith('ws'));
      if (hasWS) result.ws_open = true;
    } catch (e) {}
    try {
      const pcs = (window.__pcs = window.__pcs || []);
      if (pcs.length === 0 && window.RTCPeerConnection) {
        const orig = window.RTCPeerConnection;
        window.RTCPeerConnection = function(...args) {
          const pc = new orig(...args);
          pcs.push(pc);
          return pc;
        }
      }
      const guess = [];
      if (window.__pcs && window.__pcs.length) guess.push(...window.__pcs);
      for (const fr of Array.from(window.frames||[])) {
        try { if (fr.__pcs && fr.__pcs.length) guess.push(...fr.__pcs); } catch(e){}
      }
      for (const pc of guess) {
        try {
          const stats = await pc.getStats();
          let has = false;
          stats.forEach(r => { if (r.type === 'inbound-rtp' || r.type === 'outbound-rtp') has = true; });
          if (has) { result.webrtc_active = true; break; }
        } catch(e){}
      }
    } catch (e) {}
    resolve(result);
  } catch (e) { resolve({error: String(e)}) }
});
"""


async def is_joined_to_meeting(page) -> bool:
    try:
        res = await page.evaluate(JOINED_JS)
        return bool(res.get("joined"))
    except Exception:
        return False


async def detect_join_state(page) -> dict:
    try:
        if await is_joined_to_meeting(page):
            return {"state": "joined", "url": page.url}
        res = await page.evaluate(JOIN_STATE_JS)
        if not isinstance(res, dict):
            return {"state": "unknown", "url": page.url}
        return res
    except Exception as e:
        return {"state": "closed", "error": str(e)}


async def page_is_healthy(page, hc_cfg: dict) -> bool:
    if await is_joined_to_meeting(page):
        return True

    indicators = hc_cfg.get("indicators", [])
    selector_values = []
    want_ws = want_webrtc = False
    for ind in indicators:
        t = ind.get("type")
        if t == "selector_exists":
            vals = [s.strip() for s in ind.get("value", "").split(",") if s.strip()]
            selector_values.extend(vals)
        elif t == "websocket_open":
            want_ws = True
        elif t == "webrtc_active":
            want_webrtc = True

    js = HEALTH_JS.replace("__INDICATOR_SELECTORS__", json.dumps(selector_values))
    try:
        res = await page.evaluate(js)
        if selector_values and res.get("selector_ok"):
            return True
        if want_webrtc and res.get("webrtc_active"):
            return True
        return False
    except Exception:
        return False
