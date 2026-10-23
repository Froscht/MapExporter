"""i18n lint: every tr("…") source string in tools/ should have an entry in every tools/lang/*.json,
and the {placeholder} names of a key and of its translation must agree (a mismatch raises inside
tr() at runtime). Run from the repo root:  python3 tools/_i18n_check.py

ast is used instead of a regex because adjacent string literals are one source string:
tr("part one " "part two") — a regex only sees "part one " and reports a key that never occurs.
"""
import ast
import json
import pathlib
import re
import string
import sys

SRC = pathlib.Path(__file__).resolve().parent
LANG_DIR = SRC / "lang"
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)")


def placeholders(text: str) -> set[str]:
    return set(PLACEHOLDER.findall(text))


def collect() -> tuple[dict[str, list[str]], list[str]]:
    """German source strings passed to tr(), mapped to the files that use them.

    Also returns the files that call tr() with a computed string (checkbox lists, category names):
    there the key only exists at runtime, so an entry that no literal call shows can still be live.
    """
    used: dict[str, set[str]] = {}
    dynamic: set[str] = set()
    for f in sorted(SRC.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "tr"
                    and node.args):
                continue
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                dynamic.add(f.name)
                continue
            keys = placeholders(first.value)
            if keys and not any(k.arg for k in node.keywords):
                print(f"{f.name}:{node.lineno}: tr(\"{first.value[:50]}…\") hat "
                      f"{sorted(keys)}, aber keine Argumente — die Klammern blieben im Text stehen")
            used.setdefault(first.value, set()).add(f.name)
    return {k: sorted(v) for k, v in used.items()}, sorted(dynamic)


def main() -> int:
    used, dynamic = collect()
    print(f"{len(used)} tr()-Quelletexte in tools/"
          + (f" — in {', '.join(dynamic)} kommen auch tr(berechnete Texte) vor" if dynamic else ""))
    bad = 0
    for lf in sorted(LANG_DIR.glob("*.json")):
        table = json.loads(lf.read_text(encoding="utf-8"))
        missing = [k for k in used if k not in table]
        unused = [k for k in table if k not in used]
        wrong = []
        for k, v in table.items():
            if k in used and isinstance(v, str) and placeholders(v) != placeholders(k):
                wrong.append((k, sorted(placeholders(k)), sorted(placeholders(v))))
        print(f"\n{lf.name}: {len(table)} Einträge, {len(missing)} fehlende, "
              f"{len(unused)} nicht mehr benutzte, {len(wrong)} Platzhalter-Fehler")
        for k in missing:
            print("   fehlt :", k[:88].replace("\n", "\\n"), "|", ",".join(used[k]))
        if unused:
            note = (f" — unzuverlässig: in {', '.join(dynamic)} werden Schlüssel zur Laufzeit "
                    f"zusammengesetzt, ein Teil dieser Einträge ist also noch in Benutzung"
                    if dynamic else "")
            print(f"   unbenutzt: {len(unused)}{note}")
            for k in unused[:5]:
                print("      ", k[:80].replace("\n", "\\n"))
        for k, want, have in wrong:
            print("   Platzhalter:", k[:60].replace("\n", "\\n"), "erwartet", want, "in Übersetzung", have)
        bad += len(missing) + len(wrong)

    # a key that is in one language but not another usually means one file was not updated
    tables = {lf.name: json.loads(lf.read_text(encoding="utf-8")) for lf in LANG_DIR.glob("*.json")}
    allkeys = set().union(*[set(t) for t in tables.values()]) if tables else set()
    for name, t in tables.items():
        only_here = sorted(k for k in t if all(k not in o for n, o in tables.items() if n != name))
        if only_here:
            print(f"\n{name}: {len(only_here)} Einträge, die in keiner anderen Sprache vorkommen "
                  f"(andere Dateien nachziehen):")
            for k in only_here[:10]:
                print("   ", k[:88].replace("\n", "\\n"))
    untranslated = [(n, t[k]) for n, t in tables.items() for k in t if k in used and t[k] == k]
    if untranslated:
        print(f"\n{len(untranslated)} Einträge, die wörtlich dem deutschen Text entsprechen "
              f"(unübersetzt, aber ok als Fallback):")
        seen = set()
        for n, _ in untranslated:
            seen.add(n)
        print("   Sprachen:", ", ".join(sorted(seen)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
