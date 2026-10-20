@echo off
rem Start the MapExporter GUI (Windows) from this folder.
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

rem Find Python: the py launcher first, then python on PATH
set "PY="
where py >nul 2>nul && py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo Python 3.12+ wurde nicht gefunden. Bitte von https://www.python.org installieren
    echo ^(Haken bei "Add python.exe to PATH" setzen^) und start.bat erneut ausfuehren.
    pause
    exit /b 1
)

rem Install the dependencies on first start
%PY% -c "import Crypto, numpy, PyQt6, pyqtgraph, OpenGL, PIL" >nul 2>nul
if errorlevel 1 (
    echo Installiere Abhaengigkeiten aus requirements.txt ...
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Installation fehlgeschlagen.
        pause
        exit /b 1
    )
)

%PY% tools\arcgui.py %*
if errorlevel 1 pause
endlocal
