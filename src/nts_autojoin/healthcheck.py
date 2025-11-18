import json
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
async def page_is_healthy(page, hc_cfg: dict) -> bool:
    indicators = hc_cfg.get("indicators", [])
    selector_values = []
    want_ws = want_webrtc = False
    for ind in indicators:
        t = ind.get("type")
        if t == "selector_exists":
            vals = [s.strip() for s in ind.get("value","").split(",") if s.strip()]
            selector_values.extend(vals)
        elif t == "websocket_open":
            want_ws = True
        elif t == "webrtc_active":
            want_webrtc = True
    js = HEALTH_JS.replace("__INDICATOR_SELECTORS__", json.dumps(selector_values))
    try:
        res = await page.evaluate(js)
        ok = False
        if selector_values and res.get("selector_ok"): ok = True
        if want_ws and res.get("ws_open"): ok = True
        if want_webrtc and res.get("webrtc_active"): ok = True
        return ok
    except Exception:
        return False
