"""Download and stage verified launcher releases from GitHub Releases."""

import base64
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlparse

import requests

from core.version import APP_REPOSITORY, APP_VERSION


RELEASES_API_URL = f"https://api.github.com/repos/{APP_REPOSITORY}/releases?per_page=100"
MAX_RELEASES_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_EXECUTABLE_BYTES = 200 * 1024 * 1024
CHUNK_SIZE = 128 * 1024
EXECUTABLE_ASSET = "HombrosafioLauncher.exe"


def _version_tuple(value):
    match = re.fullmatch(r"launcher-v(\d+)\.(\d+)\.(\d+)", str(value).strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _latest_launcher_release():
    response = requests.get(
        RELEASES_API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "HombrosafioLauncherUpdater/1",
        },
        timeout=(5, 12),
    )
    response.raise_for_status()
    if len(response.content) > MAX_RELEASES_RESPONSE_BYTES:
        raise ValueError("La respuesta de versiones del launcher es demasiado grande.")

    releases = response.json()
    if not isinstance(releases, list):
        raise ValueError("GitHub devolvió una lista de versiones con formato inesperado.")

    candidates = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            continue
        version = _version_tuple(release.get("tag_name", ""))
        if version is None:
            continue

        assets = release.get("assets", [])
        if not isinstance(assets, list):
            continue
        asset = next(
            (item for item in assets if isinstance(item, dict) and item.get("name") == EXECUTABLE_ASSET),
            None,
        )
        if asset is None:
            continue

        digest = str(asset.get("digest") or "").lower()
        digest_match = re.fullmatch(r"sha256:([a-f0-9]{64})", digest)
        if not digest_match:
            # Releases without a GitHub-provided SHA-256 are ignored.
            continue

        expected_url = (
            f"https://github.com/{APP_REPOSITORY}/releases/download/"
            f"{release['tag_name']}/{EXECUTABLE_ASSET}"
        )
        download_url = asset.get("browser_download_url", "")
        parsed_url = urlparse(download_url)
        if (
            download_url != expected_url
            or parsed_url.scheme != "https"
            or parsed_url.netloc.lower() != "github.com"
        ):
            continue

        try:
            size = int(asset.get("size", 0))
        except (TypeError, ValueError):
            continue
        if size <= 0 or size > MAX_EXECUTABLE_BYTES:
            continue

        candidates.append({
            "version": version,
            "version_text": ".".join(str(part) for part in version),
            "download_url": download_url,
            "sha256": digest_match.group(1),
            "size": size,
        })

    return max(candidates, key=lambda release: release["version"], default=None)


def check_and_stage_update(
    current_version=APP_VERSION,
    executable_path=None,
    status_callback=None,
):
    """Return a verified adjacent .update file when a newer frozen Windows build exists."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return None

    current_parts = tuple(int(part) for part in current_version.split("."))
    executable_path = os.path.abspath(executable_path or sys.executable)
    executable_dir = os.path.dirname(executable_path)

    # The launcher distributes the self-updating single-file build. An onedir
    # build also needs its _internal folder replaced, so do not update it by
    # replacing only the executable.
    if os.path.isdir(os.path.join(executable_dir, "_internal")):
        return None

    if status_callback:
        status_callback("Buscando actualizaciones…", None)
    release = _latest_launcher_release()
    if release is None or release["version"] <= current_parts:
        return None

    if status_callback:
        status_callback(f"Descargando actualización {release['version_text']}…", 0.0)

    staged_path = executable_path + ".update"
    partial_path = staged_path + ".part"
    for path in (partial_path, staged_path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    digest = hashlib.sha256()
    downloaded = 0
    try:
        with requests.get(
            release["download_url"],
            headers={"User-Agent": "HombrosafioLauncherUpdater/1"},
            stream=True,
            timeout=(10, 60),
        ) as response:
            response.raise_for_status()
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) != release["size"]:
                raise ValueError("El tamaño del archivo no coincide con GitHub Releases.")

            with open(partial_path, "wb") as output:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > MAX_EXECUTABLE_BYTES:
                        raise ValueError("El archivo de actualización excede el tamaño permitido.")
                    digest.update(chunk)
                    output.write(chunk)
                    if status_callback:
                        status_callback(
                            f"Descargando actualización {release['version_text']}…",
                            min(downloaded / release["size"], 1.0),
                        )

        if downloaded != release["size"]:
            raise ValueError("El tamaño descargado no coincide con GitHub Releases.")
        if digest.hexdigest() != release["sha256"]:
            raise ValueError("La suma SHA-256 de la actualización no coincide.")

        os.replace(partial_path, staged_path)
        return staged_path
    except Exception:
        for path in (partial_path, staged_path):
            try:
                os.remove(path)
            except OSError:
                pass
        raise


def _powershell_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def start_self_update(staged_path, executable_path=None, parent_pid=None):
    """Start a detached Windows helper which replaces the EXE after this process exits."""
    if os.name != "nt":
        raise OSError("La instalación automática solo está disponible en Windows.")

    executable_path = os.path.abspath(executable_path or sys.executable)
    staged_path = os.path.abspath(staged_path)
    if os.path.dirname(staged_path).casefold() != os.path.dirname(executable_path).casefold():
        raise ValueError("El archivo actualizado debe estar junto al launcher.")
    if not os.path.isfile(staged_path):
        raise FileNotFoundError(staged_path)

    powershell = shutil.which("powershell.exe")
    if not powershell:
        raise FileNotFoundError("No se encontró Windows PowerShell para completar la actualización.")

    log_dir = os.path.join(
        os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(), "Hombrosafio"
    )
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "launcher-update.log")
    process_id = int(parent_pid or os.getpid())
    script = f"""
$ErrorActionPreference = 'Stop'
$source = {_powershell_literal(staged_path)}
$target = {_powershell_literal(executable_path)}
$backup = $target + '.previous'
$log = {_powershell_literal(log_path)}
try {{ Wait-Process -Id {process_id} -Timeout 90 -ErrorAction SilentlyContinue }} catch {{}}
$updated = $false
for ($attempt = 0; $attempt -lt 40; $attempt++) {{
    try {{
        if ([System.IO.File]::Exists($target)) {{
            if ([System.IO.File]::Exists($backup)) {{ [System.IO.File]::Delete($backup) }}
            [System.IO.File]::Replace($source, $target, $backup, $true)
        }} else {{
            [System.IO.File]::Move($source, $target)
        }}
        $updated = $true
        break
    }} catch {{ Start-Sleep -Milliseconds 750 }}
}}
if (-not $updated) {{
    Add-Content -LiteralPath $log -Value ("No se pudo reemplazar el launcher: " + (Get-Date))
    $env:PYINSTALLER_RESET_ENVIRONMENT = '1'
    $env:HOMBROSAFIO_SKIP_LAUNCHER_UPDATE = '1'
    $env:HOMBROSAFIO_UPDATE_FALLBACK = 'No se pudo instalar la actualización. Se abrió la versión actual.'
    try {{ Start-Process -FilePath $target -WindowStyle Normal }} catch {{
        Add-Content -LiteralPath $log -Value ("No se pudo abrir la versión actual: " + (Get-Date))
    }}
    exit 1
}}
$env:PYINSTALLER_RESET_ENVIRONMENT = '1'
try {{ Start-Process -FilePath $target -WindowStyle Normal }} catch {{
    Add-Content -LiteralPath $log -Value ("No se pudo reiniciar el launcher: " + (Get-Date))
    exit 1
}}
"""
    encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    subprocess.Popen(
        [powershell, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
         "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded_script],
        cwd=os.path.dirname(executable_path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
        close_fds=True,
    )
