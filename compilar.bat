@echo off
setlocal
chcp 65001 > nul
cd /d "%~dp0"
echo Compilando Hombrosafio Launcher...
python -m PyInstaller --noconfirm --onedir --windowed --name HombrosafioLauncher --icon assets\icon.ico --add-data "assets;assets" --collect-all customtkinter --collect-all minecraft_launcher_lib main.py
if errorlevel 1 goto failure
python -m PyInstaller --noconfirm --onefile --windowed --name HombrosafioLauncher --icon assets\icon.ico --add-data "assets;assets" --collect-all customtkinter --collect-all minecraft_launcher_lib main.py
if errorlevel 1 goto failure
copy /y "dist\HombrosafioLauncher.exe" "dist\HombrosafioLauncher-Portable.exe" > nul
if errorlevel 1 goto failure
copy /y "dist\HombrosafioLauncher.exe" "HombrosafioLauncher.exe" > nul
if errorlevel 1 goto failure
echo Compilacion completa: HombrosafioLauncher.exe (en esta carpeta)
pause
exit /b 0
:failure
echo La compilacion fallo. Revisa el error anterior.
pause
exit /b 1
