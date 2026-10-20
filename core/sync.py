import hashlib
import json
import os
import re
import stat
import time
import tempfile
import requests
from core.security import require_https, validate_manifest, mod_path, MAX_MANIFEST_BYTES, MAX_MOD_BYTES


def _clear_readonly(path):
    try:
        if os.path.exists(path):
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


def calculate_sha1(file_path):
    if not os.path.isfile(file_path):
        return None
    # Compatibility checksum for the pack format, not a signature/authentication.
    sha1 = hashlib.sha1(usedforsecurity=False)
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            sha1.update(chunk)
    return sha1.hexdigest()


def _get_mod_prefix(filename):
    name = filename[:-4] if filename.lower().endswith(".jar") else filename
    m = re.match(r"^([a-zA-Z0-9_\-]+?)(?:[-_]v?\d+.*)?$", name)
    if m:
        return m.group(1).lower()
    return name.lower()


def fetch_manifest(manifest_url, local_cache_dir):
    cache_path = os.path.join(local_cache_dir, "cached_manifest.json")
    try:
        require_https(manifest_url)
        resp = requests.get(manifest_url, timeout=6.0, stream=True)
        resp.raise_for_status()
        try:
            require_https(resp.url)
            payload = bytearray()
            for chunk in resp.iter_content(chunk_size=65536):
                payload.extend(chunk)
                if len(payload) > MAX_MANIFEST_BYTES:
                    raise ValueError("El manifiesto excede el límite permitido.")
            data = validate_manifest(json.loads(payload))
        finally:
            resp.close()
        os.makedirs(local_cache_dir, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return data, None
    except Exception as e:
        print(f"[Sync] Error descargando manifest de {manifest_url}: {e}")

    # Fallback to local cache if network is down
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return validate_manifest(json.load(f)), "Usando manifest en caché (sin conexión)"
        except Exception:
            pass

    return None, "No se pudo obtener el manifest ni hay caché local"


def sync_mods(manifest, game_dir, status_callback=None, progress_callback=None):
    """
    Sincroniza la carpeta mods/ con el manifest.
    Descarga mods nuevos o modificados con verificación atómica de SHA-1.
    Elimina versiones obsoletas de mods gestionados sin alterar mods adicionales del usuario.
    """
    mods_dir = os.path.join(game_dir, "mods")
    if os.path.realpath(mods_dir) != os.path.join(os.path.realpath(game_dir), "mods"):
        raise ValueError("La carpeta mods no puede redirigir a otro directorio.")
    validate_manifest(manifest)
    # Check every destination BEFORE deleting or replacing any managed file.
    for mod in manifest["mods"]:
        mod_path(mods_dir, mod["name"])
    os.makedirs(mods_dir, exist_ok=True)
    tracker_path = os.path.join(mods_dir, ".installed_manifest_mods.json")

    required_mods = manifest.get("mods") if manifest else None
    if required_mods is None or len(required_mods) == 0:
        print("[Sync] El manifiesto no contiene mods o la lista está vacía; no se modificarán los mods existentes.")
        if status_callback:
            status_callback("Manifiesto sin mods nuevos. Conservando mods locales.")
        if progress_callback:
            progress_callback(1.0)
        return

    expected_filenames = {m["name"]: m for m in required_mods if m.get("name")}
    manifest_prefixes = {_get_mod_prefix(name) for name in expected_filenames}

    # Leer registro de mods previamente gestionados por el launcher
    tracked_installed = set()
    has_tracker = os.path.isfile(tracker_path)
    if has_tracker:
        try:
            with open(tracker_path, "r", encoding="utf-8") as tf:
                tracker_data = json.load(tf)
                if isinstance(tracker_data, list):
                    tracked_installed = set(tracker_data)
                elif isinstance(tracker_data, dict):
                    tracked_installed = set(tracker_data.get("installed_files", []))
        except Exception:
            tracked_installed = set()

    # 1. Identificar y eliminar versiones obsoletas de mods gestionados
    current_files = os.listdir(mods_dir)
    to_remove = set()

    for fname in current_files:
        if not fname.lower().endswith(".jar"):
            continue
        if fname in expected_filenames:
            continue

        # Si el mod estaba registrado por el launcher en una versión anterior, es obsoleto
        if fname in tracked_installed:
            to_remove.add(fname)
            continue

        # Compatibilidad hacia atrás: limpiar hombrosafio-*.jar viejos
        if fname.lower().startswith("hombrosafio-"):
            to_remove.add(fname)
            continue

        # Si no había tracker (instalación previa de versión anterior del launcher),
        # detectar si coincide con el prefijo de algún mod del manifiesto oficial
        if not has_tracker:
            prefix = _get_mod_prefix(fname)
            if prefix in manifest_prefixes:
                to_remove.add(fname)

    # 2. Identificar qué mods requieren descarga
    mods_to_download = []
    for mod_info in required_mods:
        mod_name = mod_info.get("name")
        if not mod_name:
            continue
        target_path = os.path.join(mods_dir, mod_name)
        expected_sha1 = mod_info.get("sha1")

        if not os.path.isfile(target_path):
            mods_to_download.append(mod_info)
        else:
            current_sha1 = calculate_sha1(target_path)
            if expected_sha1 and current_sha1 and current_sha1.lower() != expected_sha1.lower():
                mods_to_download.append(mod_info)

    # 3. Descarga continua con progreso acumulado y verificación atómica
    if mods_to_download:
        total_download_bytes = sum(int(m.get("size") or 1024 * 1024) for m in mods_to_download)
        total_download_bytes = max(1, total_download_bytes)
        global_bytes_done = 0
        total_count = len(mods_to_download)

        for index, mod_info in enumerate(mods_to_download, start=1):
            mod_name = mod_info["name"]
            expected_sha1 = mod_info.get("sha1")
            download_url = mod_info.get("url")
            mod_size = int(mod_info.get("size") or 0)
            target_path = mod_path(mods_dir, mod_name)
            temp_path = None

            if status_callback:
                status_callback(f"Descargando mod ({index}/{total_count}): {mod_name}")

            download_success = False
            last_err = None
            max_retries = 3

            for attempt in range(max_retries):
                resp = None
                try:
                    resp = requests.get(download_url, stream=True, timeout=20.0)
                    resp.raise_for_status()
                    require_https(resp.url)

                    file_bytes = 0
                    fd, temp_path = tempfile.mkstemp(prefix=".download-", suffix=".tmp", dir=mods_dir)
                    with os.fdopen(fd, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=32768):
                            if chunk:
                                f.write(chunk)
                                file_bytes += len(chunk)
                                if file_bytes > MAX_MOD_BYTES:
                                    raise ValueError("La descarga del mod excede el límite permitido.")
                                if progress_callback:
                                    current_global = global_bytes_done + file_bytes
                                    progress_callback(min(1.0, current_global / total_download_bytes))

                    # Verificar integridad SHA-1 en el archivo .tmp ANTES de reemplazar el destino
                    resp.close()
                    if expected_sha1:
                        actual_sha1 = calculate_sha1(temp_path)
                        if not actual_sha1 or actual_sha1.lower() != expected_sha1.lower():
                            raise RuntimeError(
                                f"Hash SHA-1 no coincide para {mod_name}: esperado {expected_sha1}, obtenido {actual_sha1}"
                            )

                    # Reemplazo atómico con reintentos para bloqueos transitorios
                    replaced = False
                    for rep_attempt in range(5):
                        try:
                            _clear_readonly(target_path)
                            os.replace(temp_path, target_path)
                            replaced = True
                            break
                        except OSError as oerr:
                            last_err = oerr
                            _clear_readonly(target_path)
                            time.sleep(0.2 * (rep_attempt + 1))

                    if not replaced:
                        raise last_err or RuntimeError(f"No se pudo reemplazar {target_path}")

                    download_success = True
                    global_bytes_done += file_bytes or mod_size
                    print(f"[Sync] Mod actualizado exitosamente: {mod_name}")
                    break

                except Exception as e:
                    last_err = e
                    if temp_path and os.path.exists(temp_path):
                        try:
                            _clear_readonly(temp_path)
                            os.remove(temp_path)
                        except Exception:
                            pass
                    if attempt < max_retries - 1:
                        time.sleep(1.0 * (attempt + 1))
                        continue
                finally:
                    if resp is not None:
                        resp.close()

            if not download_success:
                raise RuntimeError(f"Error descargando {mod_name}: {last_err}")

    # Remove obsolete versions only after all replacements have been verified.
    for fname in to_remove:
        fpath = os.path.join(mods_dir, fname)
        if os.path.isfile(fpath):
            try:
                _clear_readonly(fpath)
                os.remove(fpath)
                print(f"[Sync] Eliminada versión obsoleta de mod: {fname}")
                if status_callback:
                    status_callback(f"Eliminando versión vieja: {fname}")
            except Exception as e:
                print(f"[Sync] No se pudo borrar {fname}: {e}")

    # 4. Actualizar registro de mods gestionados por el launcher
    try:
        with open(tracker_path, "w", encoding="utf-8") as tf:
            json.dump({"installed_files": sorted(list(expected_filenames.keys()))}, tf, indent=2)
    except Exception as e:
        print(f"[Sync] Error guardando registro de mods instalados: {e}")

    if status_callback:
        status_callback("Todos los mods están actualizados.")
    if progress_callback:
        progress_callback(1.0)
