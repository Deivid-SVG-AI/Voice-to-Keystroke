@echo off
rem Genera dist\VoiceToKeystroke.exe (un solo archivo, sin consola) con PyInstaller.
rem Uso: doble clic o "build.bat" desde la carpeta del proyecto.
rem El modelo de voz no va dentro del .exe: se descarga la primera vez a %USERPROFILE%\.cache\vosk.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creando entorno virtual .venv ...
    python -m venv .venv || goto :error
)
set "PY=.venv\Scripts\python.exe"

echo Instalando dependencias ...
"%PY%" -m pip install -q vosk sounddevice pynput soundcard pyinstaller || goto :error

echo Generando el .exe (tarda 1-2 minutos) ...
rem --collect-all vosk:    libvosk.dll y sus DLLs
rem --collect-data soundcard: cabeceras .h que soundcard lee al importarse
rem --hidden-import pynput...: pynput carga su backend de Windows dinamicamente
"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name VoiceToKeystroke ^
    --collect-all vosk ^
    --collect-data soundcard ^
    --hidden-import pynput.keyboard._win32 ^
    --hidden-import pynput.mouse._win32 ^
    --distpath dist ^
    --workpath "%TEMP%\VoiceToKeystroke-build" ^
    --specpath "%TEMP%\VoiceToKeystroke-build" ^
    "%~dp0main.py" || goto :error

rem No se copia config.json: el .exe crea el suyo junto a el al cambiar cualquier ajuste.
echo.
echo Listo: %~dp0dist\VoiceToKeystroke.exe
pause
exit /b 0

:error
echo.
echo ERROR: no se pudo generar el .exe (revisa los mensajes de arriba).
pause
exit /b 1
