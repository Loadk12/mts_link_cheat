# live.py — хранит активные страницы Chrome для команд из Telegram
from typing import Optional

_current_pages = {}  # name -> page

def set_page(name: str, page):
    _current_pages[name] = page

def clear_page(name: str):
    _current_pages.pop(name, None)

def get_any_page():
    # Берём любую активную страницу
    return next(iter(_current_pages.values()), None)

def get_pages():
    return dict(_current_pages)
