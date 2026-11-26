import json


JOINED_JS = r"""
() => {
  const contexts = [];
  const addContext = (win) => {
    try {
      const doc = win.document;
      if (!doc) return;
      contexts.push({ win, doc });
      for (let i = 0; i < win.frames.length; i += 1) addContext(win.frames[i]);
    } catch (e) {}
  };
  addContext(window);
  const visible = (el) => {
    if (!el) return false;
    const win = el.ownerDocument && el.ownerDocument.defaultView || window;
    const style = win.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
  };
  const norm = (value) => (value || "").toLowerCase();
  const textOf = (el) => norm([
    el.getAttribute("aria-label"),
    el.getAttribute("label"),
    el.getAttribute("title"),
    el.getAttribute("data-testid"),
    el.getAttribute("data-test-tag"),
    el.innerText,
    el.textContent,
  ].filter(Boolean).join(" "));
  const query = (selector) => {
    const out = [];
    for (const ctx of contexts) {
      try { out.push(...Array.from(ctx.doc.querySelectorAll(selector))); } catch(e) {}
    }
    return out;
  };

  const buttons = query("button, [role='button'], a, [aria-label], [data-testid], [data-test-tag]")
    .filter(visible);
  const labels = buttons.map(textOf);
  const bodyText = norm(contexts.map(ctx => {
    try { return ctx.doc.body ? ctx.doc.body.innerText : ""; } catch(e) { return ""; }
  }).join("\n"));
  const compactBodyText = bodyText.replace(/\s+/g, " ");
  const telemostHost = /(^|\.)telemost\.yandex\.ru$/i.test(location.hostname);

  const leaveHints = [
    "покинуть", "выйти", "выйти из встречи", "завершить", "leave", "hang up", "end call", "end-call",
  ];
  const finalJoinHints = [
    "присоединиться к встрече", "подключиться", "enter-conference", "join meeting",
  ];
  const controlHints = [
    "microphone", "camera", "video", "screensharing", "screen sharing",
    "микроф", "камер", "демонстрац", "экран", "поднять руку", "raise_hand", "raise hand",
    "participants-button", "chat-alt-button", "start-demonstration", "more-popup", "участники", "чат",
  ];

  const finalJoinButton = labels.some(label => finalJoinHints.some(h => label.includes(h)));
  const leaveButton = labels.some(label => leaveHints.some(h => label.includes(h)));
  const participantsButton = labels.some(label => label.includes("participants-button") || label.includes("участники"));
  const chatButton = labels.some(label => label.includes("chat-alt-button") || label.includes("открыть чат") || /(^|\s)чат($|\s)/.test(label));
  const controlCount = labels.filter(label => controlHints.some(h => label.includes(h))).length;
  const chatInput = !!query("[contenteditable='true'], textarea, input")
    .find(el => visible(el) && /сообщ|message|chat|введите/.test(textOf(el)));
  const mediaLayout = !!query("video, canvas, [data-testid*='Layout' i], [data-testid*='Webinar' i], [class*='webinar' i], [class*='conference' i], [class*='meeting' i]")
    .find(visible);
  const telemostContinue = telemostHost && (
    compactBodyText.includes("подключение к звонку")
    || compactBodyText.includes("вы подключаетесь к видеовстрече")
    || labels.some(label => label.includes("meeting-continue-in-browser-continue") || label.includes("продолжить в браузере"))
  );
  const telemostBlocking = telemostHost && (
    compactBodyText.includes("большое обновление в телемосте")
    || compactBodyText.includes("разрешите находить вас")
    || compactBodyText.includes("проверьте профиль")
    || compactBodyText.includes("включить видео не удалось")
    || compactBodyText.includes("включить микрофон не удалось")
  );
  const telemostPrejoin = telemostHost && !telemostContinue
    && (
      compactBodyText.includes("ваше имя на встрече")
      || labels.some(label => label.includes("подключиться") || label.includes("enter-conference"))
    );
  const telemostConnecting = telemostHost && (
    compactBodyText.includes("групповой звонок подключение")
    || (!telemostContinue && !telemostPrejoin && /(^|\s)подключение($|\s)/.test(compactBodyText))
  );
  const deviceControl = /камера|микрофон|проверить звук|camera|microphone|audio input/.test(bodyText)
    || labels.some(label => /камера|микрофон|camera|microphone/.test(label));
  const devicePreview = !!query("video, canvas, [class*='preview' i], [class*='avatar' i]").find(visible);
  const urlLooksInside = /\/(event|webinar|meeting|room|session|call)\b/i.test(location.pathname)
    && !/landing|enter|login/i.test(location.pathname);

  if (telemostBlocking || telemostContinue || telemostPrejoin || telemostConnecting || finalJoinButton || (deviceControl && devicePreview && !leaveButton)) {
    return {
      joined: false,
      telemostBlocking,
      telemostContinue,
      telemostPrejoin,
      telemostConnecting,
      finalJoinButton,
      deviceControl,
      devicePreview,
      leaveButton,
      participantsButton,
      chatButton,
      controlCount,
      chatInput,
      mediaLayout,
      url: location.href
    };
  }

  const telemostJoined = telemostHost && leaveButton && (participantsButton || chatButton);
  const joined = telemostHost
    ? telemostJoined
    : (leaveButton || (mediaLayout && (chatInput || controlCount >= 1)) || (urlLooksInside && leaveButton));
  return {
    joined,
    telemostBlocking,
    telemostContinue,
    telemostPrejoin,
    telemostConnecting,
    finalJoinButton,
    deviceControl,
    devicePreview,
    leaveButton,
    participantsButton,
    chatButton,
    controlCount,
    chatInput,
    mediaLayout,
    url: location.href
  };
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
    el.getAttribute("data-test-tag"),
    el.getAttribute("placeholder"),
    el.innerText,
    el.textContent,
  ].filter(Boolean).join(" "));

  const bodyText = norm(document.body ? document.body.innerText : "");
  const compactBodyText = bodyText.replace(/\s+/g, " ");
  const bodyLength = bodyText.trim().length;
  const url = location.href;
  const buttons = Array.from(document.querySelectorAll("button, [role='button'], a, [aria-label], [data-testid], [data-test-tag]"))
    .filter(visible);
  const labels = buttons.map(textOf);
  const telemostHost = /(^|\.)telemost\.yandex\.ru$/i.test(location.hostname);

  const joinHints = ["присоедин", "подключ", "войти", "join", "enter"];
  const finalJoinHints = ["присоединиться к встрече", "подключиться", "join meeting"];
  const joinButton = labels.some(label => joinHints.some(h => label.includes(h)));
  const finalJoinButton = labels.some(label => finalJoinHints.some(h => label.includes(h)));
  const nameInput = !!document.querySelector("#name, input[name*='name' i], input[placeholder*='имя' i], input[placeholder*='name' i]");
  const landingForm = !!document.querySelector("#EventEnterForm") || (nameInput && joinButton);
  const telemostContinue = telemostHost && (
    compactBodyText.includes("подключение к звонку")
    || compactBodyText.includes("вы подключаетесь к видеовстрече")
    || labels.some(label => label.includes("meeting-continue-in-browser-continue") || label.includes("продолжить в браузере") || label.includes("continueinbrowserbutton"))
  );
  const telemostBlocking = telemostHost && (
    compactBodyText.includes("большое обновление в телемосте")
    || compactBodyText.includes("разрешите находить вас")
    || compactBodyText.includes("проверьте профиль")
    || compactBodyText.includes("включить видео не удалось")
    || compactBodyText.includes("включить микрофон не удалось")
  );
  const telemostPrejoin = telemostHost && !telemostContinue
    && labels.some(label => label.includes("подключиться") || label.includes("enter-conference"));
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
  if (telemostBlocking) state = "telemost_overlay";
  else if (telemostPrejoin) state = "telemost_prejoin";
  else if (telemostContinue) state = "telemost_continue_browser";
  else if (finalJoinButton || (deviceControl && devicePreview && joinButton)) state = "device_prejoin";
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
    telemostContinue,
    telemostBlocking,
    telemostPrejoin,
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
