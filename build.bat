@echo off
rem ============================================================================
rem  Solina build script: solina.py -> dist\Solina\Solina.exe -> Solina_Setup.exe
rem  Run from an Anaconda Prompt with the app's environment active:
rem      conda activate solina_env
rem      build.bat
rem  Folder must contain: solina.py, Solina.iss  (optional: solina.ico, ffmpeg.exe)
rem ============================================================================
setlocal
cd /d "%~dp0"

if not exist solina.py (
    echo ERROR: solina.py not found next to build.bat
    exit /b 1
)

echo.
echo [1/4] Installing PyInstaller...
python -m pip install --upgrade pyinstaller || exit /b 1

echo.
echo [2/4] Building Solina.exe...
set ICON=
set ADDICON=
if exist solina.ico set ICON=--icon solina.ico
if exist solina.ico set ADDICON=--add-data "solina.ico;."
python -m PyInstaller --noconfirm --clean --noconsole --onedir --name Solina %ICON% %ADDICON% ^
    --hidden-import pyttsx3.drivers.sapi5 ^
    --collect-submodules comtypes ^
    --collect-all ttkbootstrap ^
    --collect-all ebooklib ^
    solina.py || exit /b 1

echo.
echo [3/4] Adding ffmpeg.exe (needed for MP3 export)...
set FF=
if exist ffmpeg.exe set "FF=%CD%\ffmpeg.exe"
if not defined FF for /f "delims=" %%F in ('where ffmpeg 2^>nul') do if not defined FF set "FF=%%F"
if not defined FF (
    echo ERROR: ffmpeg.exe not found. Put ffmpeg.exe next to build.bat or add ffmpeg to PATH.
    exit /b 1
)
copy /y "%FF%" "dist\Solina\ffmpeg.exe" >nul || exit /b 1
echo     using %FF%

echo.
echo [4/4] Building the installer with Inno Setup...
set ISCC=
rem Inno Setup 7 (64-bit or 32-bit edition) is used only if version 6 is not installed
if exist "%LocalAppData%\Programs\Inno Setup 7\ISCC.exe" set "ISCC=%LocalAppData%\Programs\Inno Setup 7\ISCC.exe"
if exist "%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 7\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 7\ISCC.exe"
if exist "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC (
    echo Inno Setup not found. Install it:  winget install JRSoftware.InnoSetup
    echo Then run build.bat again, or open Solina.iss and press Compile.
    exit /b 1
)
"%ISCC%" Solina.iss || exit /b 1

echo.
echo DONE. Installer: installer_output\Solina_Setup_1.0.0.exe
endlocal
