from __future__ import annotations

from urllib.parse import urlparse


MTS_LINK = "mts_link"
YANDEX_TELEMOST = "yandex_telemost"
GENERIC = "generic"


def detect_provider(url: str | None) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    if host == "my.mts-link.ru":
        return MTS_LINK
    if host == "telemost.yandex.ru":
        return YANDEX_TELEMOST
    return GENERIC
