#!/usr/bin/env bash
# Start the MapExporter GUI from this folder (all paths are relative to it).
cd "$(dirname "$(readlink -f "$0")")" || exit 1
exec python3 tools/arcgui.py "$@"
