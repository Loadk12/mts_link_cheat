import aiohttp

def _tg(cfg):
    n = cfg.get("notify") or {}
    return n.get("telegram_bot_token"), n.get("telegram_chat_id")

async def notify(cfg, text: str):
    token, chat = _tg(cfg)
    if not token or not chat:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=data, timeout=60) as r:
                await r.text()
    except Exception:
        pass

async def send_document(cfg, file_path: str, caption: str = ""):
    token, chat = _tg(cfg)
    if not token or not chat:
        return
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    form = aiohttp.FormData()
    form.add_field("chat_id", str(chat))
    if caption:
        form.add_field("caption", caption)
    form.add_field("document", open(file_path, "rb"), filename=file_path.split("/")[-1])
    async with aiohttp.ClientSession() as s:
        async with s.post(url, data=form, timeout=120) as r:
            await r.text()

async def send_photo(cfg, file_path: str, caption: str = ""):
    token, chat = _tg(cfg)
    if not token or not chat:
        return
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    form = aiohttp.FormData()
    form.add_field("chat_id", str(chat))
    if caption:
        form.add_field("caption", caption)
    form.add_field("photo", open(file_path, "rb"), filename=file_path.split("/")[-1])
    async with aiohttp.ClientSession() as s:
        async with s.post(url, data=form, timeout=120) as r:
            await r.text()
