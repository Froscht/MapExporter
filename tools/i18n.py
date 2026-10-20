"""GUI translations.

The source strings are German: tr("Exportieren") returns the entry from tools/lang/<code>.json
for the selected language (key = German text). Missing entries fall back to the German text.
New language: copy lang/en.json, translate it, add code + name to LANGUAGES below.
"""
import json
from pathlib import Path

LANG_DIR = Path(__file__).resolve().parent / "lang"

LANGUAGES = {            # code -> name in its own language
    "de": "Deutsch",
    "en": "English",
    "fr": "Français",
    "es": "Español",
    "it": "Italiano",
    "pl": "Polski",
    "pt": "Português (Brasil)",
    "ru": "Русский",
    "zh": "简体中文",
    "ja": "日本語",
}

_table: dict[str, str] = {}
current = "de"


def system_language() -> str:
    try:
        from PyQt6.QtCore import QLocale
        code = QLocale.system().name().split("_")[0].lower()
    except Exception:
        import locale
        code = (locale.getlocale()[0] or "en")[:2].lower()
    return code if code in LANGUAGES else "en"


def set_language(code: str) -> str:
    global _table, current
    code = code if code in LANGUAGES else system_language()
    current = code
    _table = {}
    if code != "de":
        try:
            _table = json.loads((LANG_DIR / f"{code}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _table = {}
    return code


def tr(text: str, **kw) -> str:
    s = _table.get(text) or text
    return s.format(**kw) if kw else s
