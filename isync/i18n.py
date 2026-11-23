"""Sunucunun tarayıcı dışında gösterdiği metinler için çeviri (ör. klasör seçme penceresi başlığı).

Arayüzle aynı sözlüğü kullanır: static/i18n/<dil>.json (anahtar = Türkçe metin).
Dil, arayüzde seçilip ayarlara kaydedilen "lang" değeridir.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DICT_DIR = Path(__file__).parent / "static" / "i18n"


@lru_cache(maxsize=4)
def _dict(lang: str) -> dict:
    try:
        return json.loads((DICT_DIR / f"{lang}.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def tr(text: str, lang: str | None) -> str:
    if not lang or lang == "tr":
        return text
    return _dict(lang).get(text, text)
