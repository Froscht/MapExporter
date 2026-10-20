# MapExporter — ARC Raiders Karte → GLB

Liest die Spieldateien von ARC Raiders offline (IoStore/Zen, reines Python) und baut daraus GLBs:
ganze Karten (Gebäude, Props, Vegetation, Terrain) sowie einzelne Modelle — Waffen, ARC-Bots,
Ausrüstung und komplette Charakter-Outfits. Läuft unter Windows und Linux.

## Start

- **Windows:** `start.bat` doppelklicken. Beim ersten Start werden die Python-Pakete aus
  `requirements.txt` automatisch installiert.
- **Linux:** `./start.sh`

In der GUI: Tab **Karten** → Karte wählen → Export-Optionen → **Exportieren**. Fortschritt und
Gesamtzeit stehen unter den Knöpfen, das Ergebnis liegt in `out/map/`.

## Voraussetzungen

- Python 3.10+ mit `pip install -r requirements.txt` (pycryptodome, numpy, PyQt6)
- ARC Raiders über Steam installiert. Der Paks-Ordner wird in den Steam-Bibliotheken automatisch
  gefunden (Windows über Registry + `libraryfolders.vdf`, Linux unter `~/.local/share/Steam`).
  Anderer Pfad oder neuer AES-Key nach einem Patch: in der GUI unter **Einstellungen**
  (wird in `arcpak/settings.json` gespeichert).
- Blender (4.x/5.x) nur für „Polygone vereinfachen“ und „In Blender öffnen“. Gesucht wird
  `$BLENDER`, dann `blender` im PATH, unter Windows zusätzlich
  `C:\Program Files\Blender Foundation\Blender*\blender.exe`.

## Modelle

Tab **Modelle**: Kategorie wählen (Waffen, Bots, Ausrüstung, Charaktere), Modell anklicken →
drehbare 3D-Vorschau (Maus: drehen, Mausrad: Zoom) → **Als GLB exportieren** (`out/models/<Kategorie>/`)
oder **In Blender öffnen**.

- **Waffen** werden wie im Spiel zusammengesetzt (`DA_WeaponVisuals_*`): Grundkörper plus Teile an
  ihren Knochen/Sockeln (Griff, Schlitten, Schaft, Visier …).
- **Charaktere** sind die Outfits aus `Items/Characters/Skins/Outfit/`: alle Teile in der
  Standard-Einstellung (Helm auf/ab, Kapuze … wie im Spiel voreingestellt), dazu wahlweise der
  Grundkörper und eines der 18 Gesichter.
- **Texturen** (Checkbox): die Grundfarben-Texturen werden ins GLB eingebettet (PNG), in der
  Vorschau als Vertexfarben gezeigt. Outfits bekommen die Farben ihrer Standard-Farbvariante — das
  Spiel setzt sie im Shader aus Farbmaske und Farbschema zusammen, das wird hier nachgebaut.
- **Skelett:** SkeletalMeshes werden mit Knochen und Skin-Gewichten exportiert (in Blender posierbar);
  statische Waffenteile hängen an ihrem Knochen. Outfit-Teile teilen sich ein gemeinsames Skelett.
- **Animationen** (Checkbox, Bots/Waffen/Ausrüstung): alle Clips des Skeletts werden als
  glTF-Animationen eingebettet (in Blender im Action-Editor wählbar). In der Vorschau lassen sie
  sich unter dem 3D-Fenster auswählen, abspielen und mit dem Schieber durchfahren. Unterstützt ist das
  Standard-Format der Engine; ACL-komprimierte Clips werden übersprungen. Prozedurale Bewegungen
  (z. B. das normale Laufen vieler Bots) stehen nicht als Clip in den Spieldaten.
- Geometrie ist LOD0.
- Die 3D-Vorschau braucht `pyqtgraph` und `PyOpenGL`, Texturen brauchen `Pillow` (alles in `requirements.txt`).

## Export-Optionen

| Option | Wirkung |
|---|---|
| Auf Spielbereich zuschneiden | nur der begehbare Bereich (BlockingVolumes + Flutfüllung ab PlayerStart); Karten ohne Spielbereich werden automatisch ohne Zuschnitt exportiert |
| Baumblätter entfernen | Blatt-/Nadel-Sektionen weg, Stämme bleiben |
| Effekt-Meshes entfernen | Rauchsäulen, Lichtstrahlen, Laub-Partikel (`/Game/Pioneer/Effects/`) |
| Unsichtbare Hilfsgeometrie entfernen | Licht-Geometrie und Lichtblocker, Licht-Portale, Himmels-/Wolkenkugeln, Occluder, Editor-Meshes, Grundformen mit Debug-Material |
| Decal-Flächen entfernen | Flecken, Graffiti, Risse, Fahrbahnmarkierungen — ohne Texturen nur graue Flächen |
| Polygone vereinfachen | Blender headless, Decimate je Mesh |

Im Spiel versteckte Komponenten (`bHiddenInGame` / `bVisible`, auch aus Blueprint-Vorlagen) werden
immer weggelassen.

## Sprache und Kartennamen

- GUI-Sprache oben rechts (Deutsch, English, Français, Español, Italiano, Polski, Português,
  Русский, 简体中文, 日本語). Texte in `tools/lang/<code>.json`, Schlüssel = deutscher Text.
  Neue Sprache: `en.json` kopieren, übersetzen, in `tools/i18n.py` unter `LANGUAGES` eintragen.
- Kartennamen („Dam Battlegrounds“, „Stella Montis“ …) kommen aus den String-Tabellen des Spiels.
  Übersetzte Namen stehen in `Game.locres` in den klassischen `.pak`-Dateien und brauchen den
  aktuellen AES-Key aus den Einstellungen; ohne gültigen Key bleiben die Namen englisch.

## Kommandozeile

```bash
python tools/mapexport.py placements Maps/PracticeRangeArray/
python tools/mapexport.py playarea   Maps/PracticeRangeArray/
python tools/mapexport.py glb        PracticeRangeArray --crop --ohne-blaetter --ohne-effekte --ohne-hilfsgeo --ohne-decals
python tools/blender_glb.py simplify out/map/PracticeRangeArray.glb out/map/PracticeRangeArray_vereinfacht.glb 0.25

python tools/mapexport.py all        # alle Karten nacheinander, Logs unter out/map/logs/
```

`python tools/mapexport.py` ohne Argumente zeigt alle Optionen.

## Aufbau

| Pfad | Zweck |
|---|---|
| `start.bat`, `start.sh` | Starter Windows / Linux |
| `tools/arcgui.py`, `tools/models_tab.py` | GUI (Browser, Karten-Export, Modelle, Bestand) |
| `tools/mapexport.py` | Kommandozeile: placements, playarea, glb, all |
| `tools/blender_glb.py` | Blender: Polygone vereinfachen, GLB öffnen |
| `tools/i18n.py`, `tools/lang/` | GUI-Übersetzungen |
| `arcpak/iostore.py`, `pakfile.py`, `crypto.py` | Container lesen (IoStore, klassische Paks, Theia-Entschlüsselung) |
| `arcpak/zen.py`, `usmap.py`, `game.py` | Pakete, Property-Schema, Eigenschaften und Vorlagen |
| `arcpak/mesh.py`, `uetransform.py`, `gltf.py` | StaticMesh, SkeletalMesh, Spline-Meshes, Landscape, Transformationen, GLB-Writer |
| `arcpak/models.py` | Modell-Katalog, Waffen- und Outfit-Zusammenbau, Modell-Export |
| `arcpak/anim.py` | Animationen dekodieren, Clips eines Skeletts finden |
| `arcpak/texture.py` | Texturen dekodieren (BC1–BC7), Grundfarbe eines Materials bestimmen |
| `arcpak/placements.py`, `playarea.py`, `mapglb.py` | die drei Export-Schritte |
| `arcpak/mapnames.py`, `config.py`, `globaldata.py` | Kartennamen, Einstellungen/Plattform, globale Namenstabellen |
| `arcpak/theia.c` | C-Kern der Entschlüsselung (vorgebaut: `theia_kern.dll` / `theia_kern.so`) |
| `usmap/SDK_Mappings.usmap` | Property-Schema; nach einem Spiel-Patch durch einen neuen Dump ersetzen |

`cache/` (Namenstabellen) und `out/` werden bei Bedarf erzeugt.

## Hinweise

- Schema-Quelle: `$ARC_USMAP`, sonst `usmap/SDK_Mappings.usmap`. Keine usmap-Dateien mischen.
- Bei großen Karten ist der Spielbereich eine Näherung über die NavMeshBoundsVolume-Box
  (Ausgabe „WARNUNG … Näherung“).
- Große Karten haben 60 000–150 000 Objekte; Blender öffnet sie entsprechend langsam.
- Den C-Kern neu bauen: `cc -O2 -shared -fPIC -o arcpak/theia_kern.so arcpak/theia.c`
  (Windows z. B. `zig cc -O2 -shared -target x86_64-windows-gnu -o arcpak/theia_kern.dll arcpak/theia.c`).
  Ohne Kern läuft ein deutlich langsamerer Python-Fallback.

## Lizenz

Apache License 2.0, siehe `LICENSE`. © 2026 [froscht](https://github.com/froscht).
Teile des Codes basieren auf [CUE4Parse](https://github.com/FabianFG/CUE4Parse) (ebenfalls
Apache-2.0), siehe `NOTICE`. Nicht mit Embark Studios oder Epic Games verbunden.
