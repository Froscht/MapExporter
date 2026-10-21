# MapExporter — ARC Raiders Karte → GLB

Liest die Spieldateien von ARC Raiders offline (IoStore/Zen, reines Python) und baut daraus GLBs:
ganze Karten (Gebäude, Props, Vegetation, Terrain) sowie einzelne Modelle — Waffen, ARC-Bots,
Ausrüstung und komplette Charakter-Outfits. Dazu zeigt die GUI die Klang- und Textur-Bestände des
Spiels, exportiert WAV, UEBA-Container und PNG, und im Datei-Browser gibt ein Doppelklick
Vorschau, Wiedergabe oder Export zu jeder Datei. Läuft unter Windows und Linux.

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
- **Ausrüstung** (Tab „Ausrüstung“): Rucksäcke, Werkzeug und die deployaren Rüstungs-Körper
  (Light/Medium/Heavy) mit ihren Falt-, Deploy- und Beschädigungs-Animationen.
- **Texturen** (Checkbox): die Grundfarben-Texturen werden ins GLB eingebettet (PNG), in der
  Vorschau als Vertexfarben gezeigt. Outfits bekommen die Farben ihrer Standard-Farbvariante — das
  Spiel setzt sie im Shader aus Farbmaske und Farbschema zusammen, das wird hier nachgebaut.
- **Skelett:** SkeletalMeshes werden mit Knochen und Skin-Gewichten exportiert (in Blender posierbar);
  statische Waffenteile hängen an ihrem Knochen. Outfit-Teile teilen sich ein gemeinsames Skelett.
- **Animationen** (Checkbox, Bots/Waffen/Ausrüstung): alle Clips des Skeletts werden als
  glTF-Animationen eingebettet (in Blender im Action-Editor wählbar). In der Vorschau lassen sie
  sich unter dem 3D-Fenster auswählen, abspielen und mit dem Schieber durchfahren. Unterstützt sind
  das Standard-Format der Engine, PerTrack-komprimierte Clips (z. B. Courier) und ACL-komprimierte
  Clips; additive Clips werden auf die Ruhepose gerechnet (die Basisanimation, über die das
  Spiel sie blendet, gibt es offline nicht). Prozedurale Bewegungen
  (z. B. das normale Laufen vieler Bots) stehen nicht als Clip in den Spieldaten.
- Geometrie ist LOD0.
- Die 3D-Vorschau braucht `pyqtgraph` und `PyOpenGL`, Texturen brauchen `Pillow` (alles in `requirements.txt`).

## Audio

Tab **Audio**: alle Sounds des Spiels durchblättern (≈55 000 Assets, davon ≈47 000 mit Klangdaten) —
mit Dauer, Samplerate, Kanälen und Format; filtern, einzeln oder in Serie exportieren (`out/audio/`).

Zwei Formate liegen in den Spieldaten:

- **Roh-PCM (16-bit)** — kurze Waffen-/Mechanik-Klänge: exportiert als abspielbare **WAV**.
- **BinkA** — der Sound-Codec der Engine. Er ist proprietär (RAD Game Tools); MapExporter hat
  keinen eigenen Decoder. Die Soundblöcke werden byte-genau in einen UEBA-Container (`.binka`)
  neu eingebettet — mit korrektem Header und Suchtabelle, in Unreal abspielbar. Mit der Checkbox
  „unveränderte .ubulk“ lässt sich die Klangdatei auch einfach raw kopieren.

Der Knopf **▶ Anhören** spielt den ausgewählten Klang ab: Roh-PCM sofort als WAV; BinkA dekodiert
er über das **externe FFmpeg**, dessen `binka`-Demuxer UEBA ab **Version 8.1** liest — ohne
passendes FFmpeg sagt die GUI das offen und es bleibt der .binka-Weg.

SoundCues, Attenuationen und Curves enthalten keine Klangdaten (nur Namen) — sie sind standardmäßig
ausgeblendet („Meta-Assets zeigen“).

## Texturen

Tab **Texturen**: alle gekochten Texturen (≈14 600 `T_…`-Pakete) mit Dateigröße, Vorschau und
PNG-Export (`out/textures/`). Dekodiert werden BC1/BC3/BC5 (DXT1/3/5), BC4, BC7 und die
unkomprimierten Formate — alles, was ARC verwendet. Die Größe der Vorschau ist wählbar (bis 4096 px, Standard
2048 px); das größte Mip bis zu dieser Größe wird exportiert — Texturen ohne Mip-Kette (viele
UI-Bilder) nehmen ihren einzigen Mip. Normalmaps im Zwei-Kanal-Format (BC5) zeigt die Vorschau mit
rekonstruierter Z-Komponente, so wie das Spiel sie einfärbt. Roughness und Masken sitzen oft im
Alphakanal — mit „Alphakanal behalten“ bleibt er im PNG, sonst ist das PNG dreikanalig (RGB).

**Farbrichtig behandelt:** manche Farb-Texturen sind in den Spieldaten linear gespeichert (ihr
`SRGB`-Flag ist aus) — ohne Korrektur wirken sie dunkel. Vorschau, PNG-Export und die in GLBs
eingebetteten Texturen entzerren solche Dateien per Gamma. Normal-, Rauheits- und echte Mask-
Texturen (`…_ColorMask`) bleiben unbearbeitet; heißt eine linear gespeicherte Textur dagegen nach
ihrem Item (`T_Gasmask_BaseColor`, `T_Backpack_BaseColor`), ist sie eine Farbkarte und wird
entzerrt. Der Farbraum steht als Text bei jeder Vorschau (z. B. „linear (Farbe → entzerrt)“).

## Datei-Browser: Doppelklick

Im Tab **Browser** öffnet ein Doppelklick auf eine Datei ein Info-Fenster, passend zum Dateityp:

- **Textur** → Vorschaubild, Farbraum, **Als PNG exportieren**
- **Klang** → Dauer/Rate/Kanäle, **▶ Anhören**, **Als WAV** (Roh-PCM) bzw. **Als UEBA (.binka)**
- **StaticMesh/SkeletalMesh** → **Als GLB exportieren** (direkt dekodiert, mit Texturen, nach
  `out/models/Browser/`)
- **Karte (.umap)** → Sprung zum Karten-Tab mit vorausgewählter Karte
- **Beliebiges .uasset** → Liste der Export-Klassen; **.ubulk/.uexp/.upkg** werden über ihr
  .uasset erklärt. Dazu jeweils „Rohe Datei extrahieren“ (die Containerdatei unverändert).

Engine-Anteile (anderer Mount) sind nicht über `/Game` adressierbar — dort gibt es nur die
Container-Info.

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

python tools/mapexport.py textures list  --filter=Rock
python tools/mapexport.py textures export T_Rock_Sand_01 --max=1024      # -> out/textures/
python tools/mapexport.py audio  list
python tools/mapexport.py audio  export Weapon_Mechanic_17_Var_01        # WAV oder .binka -> out/audio/
python tools/mapexport.py audio  export Grab_GuitarAcoustic_04 --wav     # BinkA-Versuch via FFmpeg (>= 8.1), sonst .binka
python tools/mapexport.py audio  export --alle --raw                     # alle .ubulk unverändert

python tools/mapexport.py all        # alle Karten nacheinander, Logs unter out/map/logs/
```

`python tools/mapexport.py` ohne Argumente zeigt alle Optionen.

## Aufbau

| Pfad | Zweck |
|---|---|
| `start.bat`, `start.sh` | Starter Windows / Linux |
| `tools/arcgui.py`, `tools/models_tab.py`, `tools/audio_tab.py`, `tools/textures_tab.py` | GUI (Browser, Karten-Export, Modelle, Audio, Texturen, Bestand) |
| `tools/mapexport.py` | Kommandozeile: placements, playarea, glb, audio, textures, all |
| `tools/blender_glb.py` | Blender: Polygone vereinfachen, GLB öffnen |
| `tools/i18n.py`, `tools/lang/` | GUI-Übersetzungen |
| `arcpak/iostore.py`, `pakfile.py`, `crypto.py` | Container lesen (IoStore, klassische Paks, Theia-Entschlüsselung) |
| `arcpak/zen.py`, `usmap.py`, `game.py` | Pakete, Property-Schema, Eigenschaften und Vorlagen |
| `arcpak/mesh.py`, `uetransform.py`, `gltf.py` | StaticMesh, SkeletalMesh, Spline-Meshes, Landscape, Transformationen, GLB-Writer |
| `arcpak/models.py` | Modell-Katalog, Waffen- und Outfit-Zusammenbau, Modell-Export |
| `arcpak/anim.py` | Animationen dekodieren, Clips eines Skeletts finden |
| `arcpak/texture.py` | Texturen dekodieren (BC1–BC7), Grundfarbe eines Materials bestimmen, Texture-Katalog, PNG-Export |
| `arcpak/audio.py` | SoundWave-Eigenschaften (Dauer, Rate, Kanäle), WAV-Export (Roh-PCM), UEBA-Container für BinkA |
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
