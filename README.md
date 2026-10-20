# Hombrosafío Launcher

Código fuente del launcher de Windows para el pack de Hombrosafío. Incluye interfaz, sincronización de mods, instalación de Java/Minecraft/Fabric y actualizaciones del launcher desde GitHub Releases.

## Desarrollo

Requiere Windows y Python 3.10 o posterior con Tkinter.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python main.py
```

Para generar el ejecutable, activar el entorno y ejecutar `compilar.bat`. Los recursos gráficos se incluyen desde `assets/`.

```powershell
.venv\Scripts\activate
compilar.bat
```

La identidad y el canal de actualización se configuran en `core/version.py`; el manifiesto y servidor predeterminados, en `core/config.py`. El repositorio de distribución del pack y de sus Releases sigue siendo independiente de este repositorio fuente. Este repositorio no publica automáticamente ejecutables.

## Comprobaciones

```powershell
.venv\Scripts\python -m unittest discover -s tests -v
.venv\Scripts\python -m bandit -r core ui main.py
.venv\Scripts\python -m pip_audit -r requirements.txt
```

La configuración y los archivos del jugador se generan en su perfil local. No se incluyen configuraciones personales, cuentas, partidas, capturas de pruebas, logs, ejecutables, claves SSH, credenciales ni carpetas de Minecraft en el repositorio. El `.gitignore` usa una lista explícita de archivos permitidos.

Los assets son recursos del proyecto; este repositorio no concede licencias sobre Minecraft, sus binarios ni los mods de terceros. No se redistribuyen esos binarios aquí.
