# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Projekt

MapExporter liest die Spieldateien von ARC Raiders offline (UE5 IoStore/Zen, reines Python + ein kleiner C-Kern) und baut daraus GLBs: ganze Karten und einzelne Modelle (Waffen, Bots, Ausrüstung, Outfits); dazu Audio- und Textur-Browser mit WAV-/UEBA-/PNG-Export. Kein pytest-Setup: `python tools/test_library_contract.py` (Guards für Bibliotheks-Vertrag, Audio-Kern und Übersetzungen — brauchen keine Spieldateien), sonst Änderungen über die CLI validieren (schnellster Test: die Practice-Range-Karte exportieren).

Lizenz: Apache-2.0; `arcpak/crypto.py`, `arcpak/theia.c` und Teile von `arcpak/mesh.py` sind von CUE4Parse abgeleitet (Details in `NOTICE`).

## Befehle

```bash
pip install -r requirements.txt        # pycryptodome, numpy, PyQt6, pyqtgraph, PyOpenGL, Pillow (Python 3.10+)

./start.sh                             # GUI starten (Linux); Windows: start.bat (installiert Deps beim ersten Start)
python tools/arcgui.py                 # dito direkt
QT_QPA_PLATFORM=offscreen python tools/arcgui.py --selftest [Suchbegriff]
                                       # schnellster GUI-Rauchtest: Fenster bauen + Screenshot nach out/

# Karten-Export (CLI). Schnellster Weg: alle drei Schritte in EINEM Prozess
python tools/mapexport.py karte PracticeRangeArray --ohne-blaetter --ohne-effekte --ohne-hilfsgeo --ohne-decals

# … oder einzeln (drei Prozesse, je ~1 s Kontext-Aufbau):
python tools/mapexport.py placements Maps/PracticeRangeArray/
python tools/mapexport.py playarea   Maps/PracticeRangeArray/
python tools/mapexport.py glb        PracticeRangeArray --crop --ohne-blaetter --ohne-effekte --ohne-hilfsgeo --ohne-decals
python tools/mapexport.py all        # alle Karten nacheinander, Logs unter out/map/logs/

# Texturen und Audio (CLI wie GUI):
python tools/mapexport.py textures list --filter=Rock
python tools/mapexport.py textures export T_Rock_Sand_01 --max=1024
python tools/mapexport.py audio list [--sort=name|dauer|groesse|format] [--format=binka|pcm] [--alle]
python tools/mapexport.py audio export Weapon_Mechanic_17_Var_01   # WAV (Roh-PCM) oder .binka (BinkA→UEBA)
python tools/mapexport.py audio export Grab_GuitarAcoustic_04 --wav  # BinkA→WAV per externem FFmpeg, sonst .binka
python tools/mapexport.py audio play  Grab_GuitarAcoustic_04       # dekodieren und sofort abspielen (~1 s)
python tools/mapexport.py audio ffmpeg [--neu] [--alle] [--ffmpeg=PFAD]  # welcher Build hier BinkA kann
python tools/mapexport.py modelle list [--filter=…] [--kategorie=…]
python tools/mapexport.py modelle export BullCrab_01 [--ohne-texturen] [--ohne-anim] [--face]

python tools/test_library_contract.py   # Guards ohne Spieldateien (in-process, ~1 s)
python tools/_i18n_check.py             # fehlende Übersetzungen, Platzhalter-Fehler, tote Schlüssel

python tools/blender_glb.py simplify out/map/Map.glb out/map/Map_vereinfacht.glb 0.25
python tools/blender_glb.py open out/map/Map.glb

# Theia-C-Kern neu bauen (optional; ohne ihn läuft ein langsamerer Python-Fallback):
cc -O2 -shared -fPIC -o arcpak/theia_kern.so arcpak/theia.c                 # Linux
zig cc -O2 -shared -target x86_64-windows-gnu -o arcpak/theia_kern.dll arcpak/theia.c  # Windows
```

`tools/mapexport.py` ohne Argumente printet alle Optionen. Ergebnisse landen in `out/` (`out/map/`, `out/models/`, `out/textures/`, `out/audio/`), alles gitignored wie `cache/`, `extracted/` und `arcpak/settings.json`.

## Architektur

Zwei Ebenen, klare Trennung:

- **`arcpak/`** — reine Bibliothek zum Lesen der Spieldaten, keine GUI-Abhängigkeiten. Wird nie direkt gestartet, sondern von `tools/` aufgerufen. **Importieren hat keine Seiteneffekte**: kein Container wird gemountet, kein `sys.argv` gelesen. Jeder Export-Schritt ist eine Funktion `run(...)` (plus `main(argv)` für die CLI).
- **`tools/`** — Einstiegspunkte. Die GUI (`arcgui.py`, `models_tab.py`, `audio_tab.py`, `textures_tab.py`) führt jeden Export-Schritt als **Subprozess** `python tools/mapexport.py <schritt>` aus; `mapexport.py` importiert das Schrittmodul und ruft `run(...)` im eigenen Prozess auf (`karte` verkettet placements → playarea → glb in **einem** Prozess). Modelle, Audio und Texturen laufen in der GUI dagegen als QThread direkt im Prozess (Katalog + Export). Beide Tools-Skripte laufen mit Repo-Root als cwd/sys.path — sie importieren `arcpak.*` und `i18n` flach (nicht `tools.i18n`). Im Browser-Tab öffnet ein Doppelklick die `FileInfoDialog` (`arcgui.py`): `InfoWorker` (QThread) klassifiziert die Datei (`map`/`textur`/`mesh`/`klang`/`uasset`/`fremd`), angeboten werden PNG-/WAV-/GLB-Export, ▶-Wiedergabe (`PlayWorker`) und Karten-Tab-Sprung. Klassifikation braucht den **vollen** Entry-Pfad `mount_point + "/" + rel` — das im Tree gespeicherte `rel` allein verliert pro Chunk Mount-Teile; `game_gpath` erkennt nur das Präfix `../../../PioneerGame/Content/` (Engine-Mounts → „fremd“).

Export-Pipeline einer Karte (drei Schritte, auch in der GUI so verkettet):

1. **`placements`** → `out/map/<Map>_placements.jsonl`: alle StaticMesh-Platzierungen einer Map. Weltransform = FTransform-Kette entlang `AttachParent` (`uetransform.py`); ISM: eine Matrix pro Instanz (Attach-Kette max. Tiefe 12). Prüft jede Mesh-Referenz gegen die gekochten Bounds der Komponente.
2. **`playarea`** → `out/map/<Map>_playarea.json`: begehbare Fläche per Flood-Fill (100-cm-Grid) ab PlayerStart, begrenzt durch BlockingVolume-Brush-Geometrie; Landscape liefert die Bodenhöhe. **Exit-Code 3 = Karte hat keinen Spielbereich** (Konstanten `EXIT_KEIN_SPIELBEREICH` / `EXIT_NO_PLAYAREA`) — GUI/`all` exportieren dann ohne Zuschnitt.
3. **`mapglb`** → `out/map/<Map>.glb`: ein Mesh pro eindeutiger Mesh-Referenz (LOD0, exakt einmal dekodiert), ein Node pro Platzierung; Filter für `bHiddenInGame`, Müll-Objekte (|Translation| > 2e6 cm), Blätter/Effekte/Hilfsgeo/Decals.

Datenzugriff von unten nach oben: `iostore.py` (utoc-Index, Block-Tabelle, Kompression) → `crypto.py` (Theia-Keystream auf Blake3-Basis; Keystream pro Datei-position verankert; Hot-Loop im C-Kern `theia_kern.*`, Python-Fallback) → `zen.py` (Zen-Pakete, Export-Tabelle) → `usmap.py` (Property-Schema) → `game.py` (kontext) → `mesh.py`/`texture.py`/`anim.py`/`audio.py` (gekochte Nutzdaten) → `placements`/`mapglb`/`models`/`playarea`/`audio` (Katalog/Export). `pakfile.py` (klassische `.pak`, nur für `Game.locres`-Übersetzungen) und `mapnames.py` (Kartennamen aus String-Tabellen + locres) bedienen nur die GUI-Namen.

## Nicht offensichtliche Konstanten und Konventionen

- **`arcpak/game.py` baut den Kontext erst bei Bedarf:** Importieren ist kostenlos; `containers()`, `global_names()`, `script_map()`, `schema()`, `templates()` mounten/parsen beim ersten Zugriff (Singletons in `_CACHE`, `reset_context()` verwirft sie). Die Modul-Attribute `C` (Container), `GN` (Namen), `smap` (ScriptObjects), `sch` (usmap-Schema) lösen über `__getattr__` (PEP 562) auf dieselben Singletons auf — `game.C` bleibt also gültig, ist aber erst beim Lesen teuer. Der Pfad zu den Paks kommt aus `config.Config.load()` (`arcpak/settings.json`, gitignored; Steam-Pfad-Autodetect in `config.find_pak_dir()`).
- **Usmap: genau ein Dump mischen-verboten.** Schema-Quelle ist `$ARC_USMAP`, sonst `usmap/SDK_Mappings.usmap`. Das Mischen zweier usmap-Dateien verschiebt Schema-Indizes und macht alle Property-Reads kaputt. Nur gezielt via `USMAP_EXTRA` erweitern. Nach einem Spiel-Patch den Dump ersetzen.
- **Unversioned Properties:** Pakete speichern nur Felder, die vom Archetyp abweichen; fehlende Werte werden über die Template-Kette (`Templates.fields` in `game.py`) aufgelöst. FReal = 8 bytes (double) wegen Large World Coordinates.
- **Koordinaten-Invariante** (`gltf.py`, `mapglb.py`): UE cm, linkshändig, Z-up → glTF m, rechtshändig, Y-up als `(x, z, y) · 0.01`; das y/z-Swap flippt die Händigkeit, Triangle-Winding bleibt unverändert — gegen Referenz-Exporte verifiziert, nicht „verbessern“.
- **Script-Import-IDs:** in `.utoc`-ScriptObjects entspricht die Import-ID dem Roh-u64 GlobalIndex inkl. Typ-Bits — nie maskieren (`globaldata.py`). Namenstabelle gecacht in `cache/globalnames.bin`, ScriptObject-Blob im System-Temp (`glob.blob`).
- **AES/Keys:** utoc/Theia-Hardcodings leben in `crypto.py`/`iostore.py`; die AES-Keys in `settings.json` gelten nur für klassische Pak-Indizes (locres). Nach Patches Keys in den GUI-Einstellungen aktualisieren, nicht im Code.
- **GUI↔Subprotokoll:** Schritte melden Fortschritt über `@@PROGRESS <done> <total>`-Zeilen auf stdout (`config.report_progress`/`parse_progress`); die GUI parst sie für die Progressbar. UTF-8: Windows-Konsolen brauchen `PYTHONUTF8`/`sys.stdout.reconfigure` (in den Einstiegspunkten erledigt).
- **`tools/blender_glb.py simplify`** nutzt einen Pool-GLB-Trick (ein Node pro glTF-Mesh statt 150 000 Nodes), decimiert pro Mesh-Datablock und schreibt die Primitives zurück — direkt importierte Karten-GLBs wären in Blender 30+ Minuten bzw. GB.
- **ARC-Audioformate (`arcpak/audio.py`):** In den `.ubulk`-Dateien stecken fast alle Klänge als BinkA-Blöcke (proprietärer Codec von RAD Game Tools — nicht selbst dekodiert; die Blöcke werden in einen UEBA-Container neu eingebettet). Erkennung: 15-byte-`'SEEK'`-Chunk mit u16-Deltas, danach Blöcke mit Rahmen `0x9999` (letzter `0xFFFF` + u32). ~320 kurze Mechanik-Klänge sind dagegen unkomprimiertes 16-bit-PCM, interleaved — erkennbar an fehlendem `'SEEK'` und `.ubulk`-Größe exakt `round(Duration×rate)×channels×2` (SoundWave-Eigenschaften) → echter WAV-Export. **UEBA-Magic:** `struct.pack("<I", 0x55454241)` — auf Platte stehen `b"ABEU"` (das u32 big-endian gelesen ist "UEBA"); Versions-Byte 1, der Spiel-Decoder prüft die Version nie.
- **BinkA-Abspielen: nicht nach Versionsnummer entscheiden.** FFmpegs `binka`-Demuxer beherrscht in allen releaseten Ständen (6.1 … 8.0.1) nur das alte 1FCB-Layout und setzt die BA2-Extradata nicht, die ARCs BA2-Blöcke brauchen → volles Rauschen statt Klang. Dekodieren kann das nur ein Build vom master-Zweig seit dem 2026-09-25-Commit (verifiziert: N-127941, 2026-10-22). Deshalb probiert `audio.ffmpeg_status()` jede gefundene Datei an einem echten Klang und prüft die Samples (`_wav_quality`: Stille, 32-Bit, falsche Länge, Vollausschlag >50 % ⇒ verwerfen); Ergebnis pro Build in `cache/ffmpeg_probe.json` (Schlüssel = mtime+Größe, ein neuer Nightly wird also neu geprüft). Suchreihenfolge: zuerst ein frisch eingegebener Pfad (`prefer`), dann `$ARC_FFMPEG`, dann `settings.json["ffmpeg"]` (Datei **oder** Ordner, via GUI-Einstellungen), dann PATH und `~/.local/bin`, `~/bin`, `~/Apps`, `/opt/ffmpeg/bin`, `/usr/local/bin`, `tools/vendor/`. `play_wav()` startet `ffplay` desselben Builds, sonst paplay/aplay/cvlc, sonst Windows-Dateiassoziierung (`''` ⇒ GUI öffnet die WAV über den Desktop). Der RAD-Codec wird nicht portiert (Lizenz).
- **GUI-Hinweise zum FFmpeg:** Audio-Tab und Datei-Dialog zeigen den Probe-Status (`ffmpeg_cached()` für Threads ohne Subprozess), `▶ Anhören` dekodiert über `playback_wav()`, und der Audio-Tab schreibt previews nach `<Systemtemp>/mapexport_klang/<name>.wav` (feste Datei pro Klang, kein Temp-Müll bei Wiederholung — `preview_path()`).
- **Textur-Katalog (`arcpak/texture.py`):** filtert nur nach Koch-Konvention `T_…` und Game-Mount; Engine-Texturen (`../../../Engine/Content/…`) haben einen anderen Mount und sind über `/Game`-Pfade nicht adressierbar. **Mip-Herkunft:** die 16-byte-Mip-Records im Paket gehören nur zu den .ubulk-Mips — voll inline gekochte Texturen haben gar keine Records; verlässlich ist die Bulk-Data-Map (ein Eintrag pro Mip, Mip-Reihenfolge), deren Größen gegen SizeX/SizeY-Halbung validiert werden (inline-SerialOffset zählt ab Export-Daten, .ubulk-Flags 0x100 ab der .ubulk-Datei; viele UI-Textur-Pakete haben genau einen Mip, der auch über `--max` hinaus benutzt wird). Vorschau von BC5-Zwei-Kanal-Normalmaps rekonstruiert die Z-Komponente nur fürs Display. Roughness-/Mask-Texturen tragen ihre Nutzdaten oft im Alphakanal — ohne `--alpha` / Checkbox fällt er weg. **Farbraum:** gekochte Texture2D schreiben `SRGB` nur bei Abweichung vom Archetyp — fehlend ⇒ sRGB (roh ansehbar), `SRGB=0` ⇒ linear gespeichert (wirkt dunkel). `load_texture` liefert roh (material_albedo braucht lineare Werte); `for_display` entzerrt per Gamma nur in Anzeige-Pfaden (Vorschau, PNG, GLB-Einbettung), namensgesteuert — eindeutige Farb-Wörter (BASECOLOR/ALBEDO/DIFFUSE/DECALCOLOR) gewinnen dabei gegen Daten-Wörter (`T_Facemask_BaseColor` ist echtes Albedo, `…_ColorMask` bleibt Daten; reines `COLOR`/`COLOUR` nur ohne Daten-Wort) — und nie bei Normalmaps (`CompressionSettings …:1` = TC_Normalmap).
- **i18n:** Quell-Sprache der GUI ist Deutsch; `tools/lang/<code>.json` hat deutsche Texte als Keys, `tr()` fällt auf Deutsch zurück. Neue Sprache: `en.json` kopieren + Eintrag in `tools/i18n.py` → `LANGUAGES`. **Jeder neue `tr()`-Text braucht den Eintrag in allen Sprachdateien** (sonst steht im Englischen/Russischen plötzlich Deutsch): `python tools/_i18n_check.py` listet fehlende Keys ( Keys aus aneinandergesetzten Literalen erkennt `ast`, nicht das Regex), `TestTranslations` in `tools/test_library_contract.py` hält die Schlüsselmengen aller Dateien gleich und prüft, dass `{platzhalter}` beim Übersetzen nicht verloren gehen. `SettingsDialog` setzt beim Öffnen die Sprache aus `settings.json` — nicht nur `i18n.set_language()` aufrufen, wenn man das in einem Test prüft.
- Code-Kommentare und Docstrings sind Englisch, User-visible Strings Deutsch (via `tr()`). `.gitattributes`: `start.bat` CRLF, `start.sh` LF; `*.dll`/`*.so`/`*.usmap` binary.
