import re, json, pathlib
src = pathlib.Path("tools")
used = {}
for f in src.glob("*.py"):
    for m in re.finditer(r'tr\(\s*"([^"]+)"', f.read_text(encoding="utf-8")):
        used.setdefault(m.group(1), set()).add(f.name)
en = json.loads((src / "lang" / "en.json").read_text(encoding="utf-8"))
missing = [k for k in used if k not in en]
print("Fehlende Uebersetzungen in en.json:")
for k in missing:
    print("  ", k[:90], "|", sorted(used[k]))
unused = [k for k in en if k not in used]
print("\nNicht mehr benutzte Eintraege:", len(unused))
for k in unused[:15]:
    print("  ", k[:80])
