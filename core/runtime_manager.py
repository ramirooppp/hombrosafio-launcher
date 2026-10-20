import json
import lzma
import os
import shutil
import stat
import subprocess
import threading
import time
import zipfile
import re
import requests
import minecraft_launcher_lib
import minecraft_launcher_lib._helper as _mll_helper
import minecraft_launcher_lib.fabric as _mll_fabric
import minecraft_launcher_lib.install as _mll_install
import minecraft_launcher_lib.runtime as _mll_runtime


_DOWNLOAD_LOCKS_GUARD = threading.Lock()
_DOWNLOAD_LOCKS = {}


def _get_path_lock(path):
    key = os.path.normcase(os.path.abspath(path))
    with _DOWNLOAD_LOCKS_GUARD:
        lock = _DOWNLOAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _DOWNLOAD_LOCKS[key] = lock
        return lock


def _no_window_flags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0


def clean_java_env():
    """
    Elimina variables de entorno globales (_JAVA_OPTIONS, JAVA_TOOL_OPTIONS, JDK_JAVA_OPTIONS, CLASSPATH)
    que muchos tutoriales o launchers de terceros como TLauncher agregan en Windows
    (ej. -Xmx512M, flags de Java 8 o classpaths corruptos) y que provocan fallos de JVM o de librerías.
    """
    env = os.environ.copy()
    for var in (
        "_JAVA_OPTIONS",
        "JAVA_TOOL_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "CLASSPATH",
        "QT_QPA_PLATFORM",
    ):
        env.pop(var, None)
    for k in list(env.keys()):
        if k.upper().startswith("TLAUNCHER") or k.upper().startswith("TL_"):
            env.pop(k, None)
    return env


def _clear_readonly(path):
    try:
        if os.path.exists(path):
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


# Prevent do_version_install from redundantly reinstalling java-runtime-epsilon
# into .minecraft/runtime 3 times during Fabric installation (which triggers
# PermissionError on bin/ucrtbase.dll when .minecraft/runtime is locked/in use).
if hasattr(_mll_install, "install_jvm_runtime"):
    _mll_install.install_jvm_runtime = lambda *args, **kwargs: None

# Harden get_sha1_hash and download_file against concurrent thread races in
# install_libraries, transient Windows Defender locks, and locked runtime DLLs.
if not getattr(_mll_helper, "_hombrosafio_patched_v2", False):
    _orig_get_sha1_hash = getattr(_mll_helper, "_orig_get_sha1_hash", _mll_helper.get_sha1_hash)
    _mll_helper._orig_get_sha1_hash = _orig_get_sha1_hash

    def _safe_get_sha1_hash(path):
        last_err = None
        for attempt in range(5):
            try:
                return _orig_get_sha1_hash(path)
            except PermissionError as err:
                last_err = err
                _clear_readonly(path)
                time.sleep(0.15 * (attempt + 1))
        raise last_err

    def _safe_download_file(url, path, callback={}, sha1=None, lzma_compressed=False,
                            session=None, minecraft_directory=None, overwrite=False):
        if minecraft_directory is not None:
            _mll_helper.check_path_inside_minecraft_directory(minecraft_directory, path)

        path_lock = _get_path_lock(path)
        with path_lock:
            if os.path.isfile(path) and not overwrite:
                try:
                    if sha1 is None:
                        if os.path.getsize(path) > 0:
                            if path.lower().endswith(".jar"):
                                try:
                                    with zipfile.ZipFile(path, "r") as zf:
                                        if len(zf.namelist()) > 0:
                                            return False
                                except Exception:
                                    pass  # Archivo corrupto o incompleto; forzar re-descarga limpia
                            else:
                                return False
                    elif _safe_get_sha1_hash(path) == sha1:
                        return False
                except OSError:
                    pass

            parent_dir = os.path.dirname(path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)

            if callback:
                callback.get("setStatus", _mll_helper.empty)("Download " + os.path.basename(path))

            headers = {"user-agent": _mll_helper.get_user_agent()}
            req = session or requests
            last_err = None
            max_attempts = 4

            for attempt in range(max_attempts):
                temp_path = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
                try:
                    resp = req.get(url, stream=True, headers=headers, timeout=(10, 60))
                    if resp.status_code in (429, 500, 502, 503, 504):
                        last_err = requests.HTTPError(f"HTTP {resp.status_code} al descargar {url}", response=resp)
                        if attempt < max_attempts - 1:
                            time.sleep(0.5 * (2 ** attempt))
                            continue
                        raise last_err
                    resp.raise_for_status()

                    with open(temp_path, "wb") as f:
                        resp.raw.decode_content = True
                        if lzma_compressed:
                            f.write(lzma.decompress(resp.content))
                        else:
                            shutil.copyfileobj(resp.raw, f)

                    if sha1 is not None:
                        checksum = _safe_get_sha1_hash(temp_path)
                        if checksum != sha1:
                            raise _mll_helper.InvalidChecksum(url, path, sha1, checksum)

                    # Replace target file with retries for transient AV locks
                    replaced = False
                    for rep_attempt in range(5):
                        try:
                            _clear_readonly(path)
                            os.replace(temp_path, path)
                            replaced = True
                            break
                        except (PermissionError, OSError) as perr:
                            last_err = perr
                            _clear_readonly(path)
                            time.sleep(0.2 * (rep_attempt + 1))

                    if not replaced:
                        if not overwrite and os.path.isfile(path) and os.path.getsize(path) > 0:
                            if sha1 is None or _safe_get_sha1_hash(path) == sha1:
                                return False
                        raise last_err or PermissionError(f"No se pudo reemplazar {path}")

                    return True
                except (requests.RequestException, _mll_helper.InvalidChecksum, lzma.LZMAError, PermissionError, OSError) as err:
                    last_err = err
                    if attempt < max_attempts - 1:
                        time.sleep(0.5 * (2 ** attempt))
                        continue
                    if not overwrite and os.path.isfile(path) and os.path.getsize(path) > 0:
                        try:
                            if sha1 is None or _safe_get_sha1_hash(path) == sha1:
                                return False
                        except Exception:
                            pass
                    raise
                finally:
                    if os.path.exists(temp_path):
                        try:
                            _clear_readonly(temp_path)
                            os.remove(temp_path)
                        except OSError:
                            pass

            if last_err is not None:
                raise last_err
            raise RuntimeError(f"Fallo al descargar {url} hacia {path}")

    _mll_helper.get_sha1_hash = _safe_get_sha1_hash
    _mll_helper.download_file = _safe_download_file
    _mll_helper._hombrosafio_patched_v2 = True

    for _mod in (_mll_runtime, _mll_install, _mll_fabric):
        if hasattr(_mod, "get_sha1_hash"):
            _mod.get_sha1_hash = _safe_get_sha1_hash
        if hasattr(_mod, "download_file"):
            _mod.download_file = _safe_download_file


def _make_mll_progress_callback(progress_callback):
    if not progress_callback:
        return {}
    state = {"max": 0}

    def set_max(max_val):
        try:
            state["max"] = int(max_val)
        except (TypeError, ValueError):
            state["max"] = 0

    def set_progress(current, max_val=None):
        total = max_val if max_val is not None else state["max"]
        if total and total > 0:
            progress_callback(max(0.0, min(1.0, float(current) / float(total))))

    return {
        "setMax": set_max,
        "setProgress": set_progress,
        "set_progress": set_progress,
    }


def check_java_version(java_exe):
    try:
        res = subprocess.run(
            [java_exe, "-version"],
            capture_output=True,
            text=True,
            timeout=5,
            env=clean_java_env(),
            creationflags=_no_window_flags(),
        )
        out = (res.stdout + res.stderr).lower()
        if res.returncode == 0 and ("version \"25" in out or "version 25" in out or "build 25" in out):
            return True
    except Exception:
        pass
    return False


def _runtime_javaw_paths(base_dir):
    paths = []
    for platform in ("windows-x64", "windows", "windows-arm64"):
        paths.append(
            os.path.join(
                base_dir,
                "runtime",
                "java-runtime-epsilon",
                platform,
                "java-runtime-epsilon",
                "bin",
                "javaw.exe",
            )
        )
    return paths


def _runtime_javaw_path(base_dir):
    for path in _runtime_javaw_paths(base_dir):
        if os.path.isfile(path):
            return path
    return os.path.join(
        base_dir,
        "runtime",
        "java-runtime-epsilon",
        "windows-x64",
        "java-runtime-epsilon",
        "bin",
        "javaw.exe",
    )


def find_or_install_java_25(hombrosafio_dir, status_callback=None, progress_callback=None):
    """
    Busca Java 25 en las ubicaciones típicas de Windows/Minecraft.
    Si no existe o está incompleto, lo descarga directamente de Mojang (java-runtime-epsilon).
    """
    candidates = []

    # 1. Launcher de Minecraft oficial (Microsoft Store)
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        store_root = os.path.join(
            local_app_data,
            "Packages",
            "Microsoft.4297127D64EC6_8wekyb3d8bbwe",
            "LocalCache",
            "Local",
        )
        candidates.extend(_runtime_javaw_paths(store_root))

    # 2. .minecraft oficial en Roaming
    app_data = os.environ.get("APPDATA", "")
    if app_data:
        candidates.extend(_runtime_javaw_paths(os.path.join(app_data, ".minecraft")))

    # 3. .hombrosafio runtime (y fallbacks aislados)
    local_java_dir = os.path.join(local_app_data or hombrosafio_dir, "HombrosafioJava25")
    candidates.extend(_runtime_javaw_paths(hombrosafio_dir))
    candidates.extend(_runtime_javaw_paths(os.path.join(hombrosafio_dir, "runtime_local")))
    candidates.extend(_runtime_javaw_paths(local_java_dir))

    # Probar candidatos verificando que el runtime funcione realmente
    for c in candidates:
        if os.path.isfile(c) and check_java_version(c):
            return c

    # 4. Probar Java en PATH si es versión 25
    for binary_name in ("javaw.exe", "java.exe"):
        try:
            res = subprocess.run(
                ["where", binary_name],
                capture_output=True,
                text=True,
                timeout=5,
                env=clean_java_env(),
                creationflags=_no_window_flags(),
            )
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    line = line.strip()
                    if line and os.path.isfile(line) and check_java_version(line):
                        return line
        except Exception:
            pass

    # Si no se encontró, descargarlo automáticamente de Mojang
    if status_callback:
        status_callback("Descargando Java 25 (OpenJDK Epsilon)...")

    callback_dict = _make_mll_progress_callback(progress_callback)

    for target_root in (
        hombrosafio_dir,
        os.path.join(hombrosafio_dir, "runtime_local"),
        local_java_dir,
    ):
        os.makedirs(target_root, exist_ok=True)
        try:
            minecraft_launcher_lib.runtime.install_jvm_runtime(
                "java-runtime-epsilon",
                target_root,
                callback=callback_dict,
            )
        except PermissionError:
            for installed_exe in _runtime_javaw_paths(target_root):
                if os.path.isfile(installed_exe) and check_java_version(installed_exe):
                    return installed_exe
            continue

        for installed_exe in _runtime_javaw_paths(target_root):
            if os.path.isfile(installed_exe) and check_java_version(installed_exe):
                return installed_exe

    # Fallback genérico a java comprobando que sea Java 25
    if check_java_version("java"):
        return "java"

    raise RuntimeError(
        "No se encontró ni se pudo descargar Java 25 (requerido para Minecraft 26.3). "
        "Por favor, comprobá tu conexión a internet o instalá Java 25 manualmente."
    )


def _is_valid_vanilla_json(json_path, game_version):
    if not os.path.isfile(json_path):
        return False
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("id") != game_version or data.get("inheritsFrom"):
            return False
        client_dl = data.get("downloads", {}).get("client", {})
        if not isinstance(client_dl, dict) or not client_dl.get("url") or int(client_dl.get("size", 0)) <= 0:
            return False
        args = data.get("arguments", {})
        if not isinstance(args, dict):
            return False
        jvm_args = args.get("jvm")
        game_args = args.get("game")
        if not isinstance(jvm_args, list) or not isinstance(game_args, list):
            return False
        if "${classpath}" not in json.dumps(jvm_args):
            return False
        libs = data.get("libraries")
        if not isinstance(libs, list) or len(libs) < 10:
            return False
        for lib in libs:
            lib_name = str(lib.get("name", "")).lower()
            if "tlauncher" in lib_name or "tlloader" in lib_name or "asm-all" in lib_name:
                return False
        return True
    except Exception:
        return False


REQUIRED_FABRIC_LIBRARIES = (
    "org.ow2.asm:asm:9.10.1",
    "org.ow2.asm:asm-analysis:9.10.1",
    "org.ow2.asm:asm-commons:9.10.1",
    "org.ow2.asm:asm-tree:9.10.1",
    "org.ow2.asm:asm-util:9.10.1",
    "net.fabricmc:sponge-mixin:",
    "net.fabricmc:fabric-loader:0.19.5",
)

PRISTINE_FABRIC_PROFILE_0_19_5_26_3 = {
    "id": "fabric-loader-0.19.5-26.3",
    "inheritsFrom": "26.3",
    "releaseTime": "2026-10-16T15:06:33+0000",
    "time": "2026-10-16T15:06:33+0000",
    "type": "release",
    "mainClass": "net.fabricmc.loader.impl.launch.knot.KnotClient",
    "arguments": {
        "game": [],
        "jvm": [
            "-DFabricMcEmu= net.minecraft.client.main.Main "
        ]
    },
    "libraries": [
        {
            "name": "org.ow2.asm:asm:9.10.1",
            "url": "https://maven.fabricmc.net/",
            "md5": "9cb72080438acf27f2607002e1444a7b",
            "sha1": "ada2141c0cc52ee8f5c48cd5fa4ce0e794f22236",
            "sha256": "ed825d10ab1399c8c0cb669e688cf0c8c82629b4c8399b58352b68e92ca10fcb",
            "size": 126151
        },
        {
            "name": "org.ow2.asm:asm-analysis:9.10.1",
            "url": "https://maven.fabricmc.net/",
            "md5": "6dd9011ec87f65e4b2e14e938c4118e4",
            "sha1": "8d49f14d51f632cb1d87c88d1ceaf50db0d8af1b",
            "sha256": "dede75a21306b65974ecd8f87114ff6970f09fb794157a4ca09ab25c888c2bfc",
            "size": 35140
        },
        {
            "name": "org.ow2.asm:asm-commons:9.10.1",
            "url": "https://maven.fabricmc.net/",
            "md5": "8eee0b1bdd046baf472eb83dd5e6bb44",
            "sha1": "4229e4c55fd8e01c23f9fe9884075cc628aacc50",
            "sha256": "6d0abefb7cbf972ea16edb37ec14835372505063a45f976ab7ea889ed9497895",
            "size": 74840
        },
        {
            "name": "org.ow2.asm:asm-tree:9.10.1",
            "url": "https://maven.fabricmc.net/",
            "md5": "62541e1dc62331408ef31317555bf4fa",
            "sha1": "e244332a17564c1d1572449399a842de35881be2",
            "sha256": "3dfb0d5b6a106cd40b5b250e39935fbf2f927f4477546a5369a3ac609cf0506b",
            "size": 51958
        },
        {
            "name": "org.ow2.asm:asm-util:9.10.1",
            "url": "https://maven.fabricmc.net/",
            "md5": "d0892ecb209867805e7d2418e5dc6c42",
            "sha1": "7bb9d450e8d4cbf9f9e04096c44bbfe7fba80b15",
            "sha256": "1bb99d091fba2597dc6d51193e9bbcf0d8447e7ed96bd8f0198b18152f09655c",
            "size": 95628
        },
        {
            "name": "net.fabricmc:sponge-mixin:0.17.4+mixin.0.8.7",
            "url": "https://maven.fabricmc.net/",
            "md5": "9e33b145b2a46fbc70daf532e3705066",
            "sha1": "5f66cc9f59b8efaa942155a3d5a30599bf6640dd",
            "sha256": "1f0ae44db7295f8626f33b1dc0ad7f043d8954a8d6847247875fcc5dfcecc934",
            "size": 1539080
        },
        {
            "name": "net.fabricmc:fabric-loader:0.19.5",
            "url": "https://maven.fabricmc.net/",
            "sha1": "ff9e65cffca4a67f31523e1807fe0855940fcbfa",
            "size": 1984980
        }
    ]
}


def _is_valid_fabric_json(json_path, game_version, target_version):
    if not os.path.isfile(json_path):
        return False
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("id") != target_version or data.get("inheritsFrom") != game_version:
            return False
        if "KnotClient" not in str(data.get("mainClass", "")):
            return False
        libs = data.get("libraries", [])
        if not isinstance(libs, list):
            return False
        lib_names = [str(item.get("name", "")) for item in libs]
        for name in lib_names:
            nl = name.lower()
            if "tlauncher" in nl or "tlloader" in nl or "asm-all" in nl:
                return False
        for req in REQUIRED_FABRIC_LIBRARIES:
            if not any(name.startswith(req) for name in lib_names):
                return False
        # Si TLauncher inyectó archivos adicionales en la carpeta de la versión, invalidar
        version_dir = os.path.dirname(json_path)
        if (
            os.path.isfile(os.path.join(version_dir, "TLauncherAdditional.json"))
            or os.path.exists(os.path.join(version_dir, "tlLoader"))
            or os.path.isfile(os.path.join(version_dir, "skinversion.json"))
        ):
            return False
        return True
    except Exception:
        return False


def _is_valid_client_jar(jar_path, expected_size=None):
    if not os.path.isfile(jar_path):
        return False
    try:
        size = os.path.getsize(jar_path)
        if size <= 0:
            return False
        if expected_size and size != expected_size:
            return False
        with zipfile.ZipFile(jar_path, "r") as zf:
            names = set(zf.namelist())
            return (
                "net/minecraft/client/main/Main.class" in names
                or "net/minecraft/client/Minecraft.class" in names
            )
    except Exception:
        return False


def _sync_inherited_client_jar(mc_dir, game_version, target_version):
    """
    minecraft_launcher_lib coloca versions/<target_version>/<target_version>.jar
    al final del classpath. Si el jugador instaló Fabric con el instalador oficial
    o si una instalación previa se interrumpió, ese JAR no existe aunque
    versions/<game_version>/<game_version>.jar sí esté presente.
    """
    base_json_path = os.path.join(mc_dir, "versions", game_version, f"{game_version}.json")
    base_jar_path = os.path.join(mc_dir, "versions", game_version, f"{game_version}.jar")
    target_dir = os.path.join(mc_dir, "versions", target_version)
    target_jar_path = os.path.join(target_dir, f"{target_version}.jar")

    expected_size = None
    client_dl = None
    if os.path.isfile(base_json_path):
        try:
            with open(base_json_path, "r", encoding="utf-8") as f:
                base_data = json.load(f)
            client_dl = base_data.get("downloads", {}).get("client", {})
            expected_size = client_dl.get("size")
        except Exception:
            expected_size = None

    base_valid = _is_valid_client_jar(base_jar_path, expected_size=expected_size)
    target_valid = _is_valid_client_jar(target_jar_path, expected_size=expected_size)

    if base_valid and not target_valid:
        os.makedirs(target_dir, exist_ok=True)
        try:
            _clear_readonly(target_jar_path)
            shutil.copy2(base_jar_path, target_jar_path)
            target_valid = _is_valid_client_jar(target_jar_path, expected_size=expected_size)
        except OSError:
            pass
    elif target_valid and not base_valid:
        os.makedirs(os.path.dirname(base_jar_path), exist_ok=True)
        try:
            _clear_readonly(base_jar_path)
            shutil.copy2(target_jar_path, base_jar_path)
            base_valid = _is_valid_client_jar(base_jar_path, expected_size=expected_size)
        except OSError:
            pass

    if not base_valid and os.path.isfile(base_jar_path):
        try:
            _clear_readonly(base_jar_path)
            os.remove(base_jar_path)
        except OSError:
            pass

    if not target_valid and os.path.isfile(target_jar_path):
        try:
            _clear_readonly(target_jar_path)
            os.remove(target_jar_path)
        except OSError:
            pass

    return base_valid and target_valid


def _ensure_client_jar_downloaded(mc_dir, game_version, target_version, callback_dict):
    if _sync_inherited_client_jar(mc_dir, game_version, target_version):
        return True

    base_json_path = os.path.join(mc_dir, "versions", game_version, f"{game_version}.json")
    base_jar_path = os.path.join(mc_dir, "versions", game_version, f"{game_version}.jar")
    if os.path.isfile(base_json_path):
        try:
            with open(base_json_path, "r", encoding="utf-8") as f:
                base_data = json.load(f)
            client_dl = base_data.get("downloads", {}).get("client", {})
            if client_dl.get("url"):
                _safe_download_file(
                    client_dl["url"],
                    base_jar_path,
                    callback=callback_dict,
                    sha1=client_dl.get("sha1"),
                    minecraft_directory=mc_dir,
                    overwrite=True,
                )
        except Exception:
            pass

    return _sync_inherited_client_jar(mc_dir, game_version, target_version)


def _sanitize_version_json(json_path):
    if not os.path.isfile(json_path):
        return False
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            d = json.load(f)
        changed = False
        for section in ("jvm", "game"):
            if isinstance(d.get("arguments", {}).get(section), list):
                for item in d["arguments"][section]:
                    if isinstance(item, dict) and "values" in item and "value" not in item:
                        item["value"] = item["values"]
                        changed = True
        if changed:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=2)
        return True
    except Exception:
        return False


def _clean_tlauncher_profile_artifacts(versions_dir):
    """Elimina artefactos inyectados por TLauncher en la carpeta de la versión."""
    if not os.path.isdir(versions_dir):
        return
    has_tl_artifact = False
    for name in ("TLauncherAdditional.json", "tlLoader", "skinversion.json"):
        p = os.path.join(versions_dir, name)
        if os.path.isfile(p):
            has_tl_artifact = True
            try:
                _clear_readonly(p)
                os.remove(p)
            except OSError:
                pass
        elif os.path.isdir(p):
            has_tl_artifact = True
            try:
                shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass
    if has_tl_artifact:
        ver_name = os.path.basename(versions_dir)
        ver_json = os.path.join(versions_dir, f"{ver_name}.json")
        if os.path.isfile(ver_json):
            try:
                _clear_readonly(ver_json)
                os.remove(ver_json)
            except OSError:
                pass


def _clean_tlauncher_global_conflicts(mc_dir):
    """
    Purga librerías legacy y carpetas conflictivas creadas globalmente por TLauncher
    en .minecraft (ej. asm-all, asm-debug-all y root tlLoader).
    """
    if not os.path.isdir(mc_dir):
        return
    # 1. Eliminar asm-all y asm-debug-all de libraries/org/ow2/asm
    asm_org_dir = os.path.join(mc_dir, "libraries", "org", "ow2", "asm")
    if os.path.isdir(asm_org_dir):
        for legacy_dir in ("asm-all", "asm-debug-all"):
            legacy_path = os.path.join(asm_org_dir, legacy_dir)
            if os.path.exists(legacy_path):
                try:
                    shutil.rmtree(legacy_path, ignore_errors=True)
                except OSError:
                    pass
    # 2. Eliminar tlLoader en la raíz de mc_dir
    tl_root = os.path.join(mc_dir, "tlLoader")
    if os.path.exists(tl_root):
        try:
            shutil.rmtree(tl_root, ignore_errors=True)
        except OSError:
            pass


def _verify_and_repair_fabric_libraries(libraries, mc_dir, callback_dict=None):
    """
    Verifica e instala directamente cada una de las librerías de Fabric y ASM
    con validación estricta de hash SHA-1, tamaño e integridad del archivo ZIP/JAR.
    Garantiza que asm-util (CheckClassAdapter) y el resto de dependencias nunca
    queden omitidas, incompletas o corruptas por interferencias de otros launchers como TLauncher.
    """
    known_names = {lib.get("name") for lib in libraries if isinstance(lib, dict) and lib.get("name")}
    combined_libs = list(libraries)
    for req_meta in PRISTINE_FABRIC_PROFILE_0_19_5_26_3["libraries"]:
        if req_meta.get("name") not in known_names:
            combined_libs.append(req_meta)

    for lib in combined_libs:
        name = lib.get("name", "")
        if not name:
            continue
        try:
            lib_path = minecraft_launcher_lib.command.get_library_path(name, mc_dir)
        except Exception:
            continue

        url_base = lib.get("url", "https://maven.fabricmc.net/").rstrip("/")
        parts = name.split(":")
        if len(parts) < 3:
            continue
        group_path = parts[0].replace(".", "/")
        artifact_id = parts[1]
        version = parts[2].split("@")[0]
        ext = parts[2].split("@")[1] if "@" in parts[2] else "jar"
        jar_name = f"{artifact_id}-{version}.{ext}"
        dl_url = f"{url_base}/{group_path}/{artifact_id}/{version}/{jar_name}"
        expected_sha1 = lib.get("sha1")
        expected_size = lib.get("size")

        needs_download = False
        if not os.path.isfile(lib_path) or os.path.getsize(lib_path) <= 0:
            needs_download = True
        elif expected_size and os.path.getsize(lib_path) != expected_size:
            needs_download = True
        elif expected_sha1 and _safe_get_sha1_hash(lib_path) != expected_sha1:
            needs_download = True
        elif lib_path.lower().endswith(".jar"):
            try:
                with zipfile.ZipFile(lib_path, "r") as zf:
                    namelist = set(zf.namelist())
                    if not namelist:
                        needs_download = True
                    elif artifact_id == "asm-util" and "org/objectweb/asm/util/CheckClassAdapter.class" not in namelist:
                        needs_download = True
                    elif artifact_id == "asm" and "org/objectweb/asm/ClassReader.class" not in namelist:
                        needs_download = True
                    elif artifact_id == "sponge-mixin" and "org/spongepowered/asm/launch/MixinBootstrap.class" not in namelist:
                        needs_download = True
                    elif artifact_id == "fabric-loader" and "net/fabricmc/loader/impl/launch/knot/KnotClient.class" not in namelist:
                        needs_download = True
            except Exception:
                needs_download = True

        if needs_download:
            try:
                _clear_readonly(lib_path)
                if os.path.isfile(lib_path):
                    os.remove(lib_path)
            except OSError:
                pass
            _safe_download_file(
                dl_url,
                lib_path,
                callback=callback_dict or {},
                sha1=expected_sha1,
                minecraft_directory=mc_dir,
                overwrite=True,
            )


def _is_fabric_installation_complete(mc_dir, game_version, target_version):
    base_json_path = os.path.join(mc_dir, "versions", game_version, f"{game_version}.json")
    target_json_path = os.path.join(mc_dir, "versions", target_version, f"{target_version}.json")

    if not _is_valid_vanilla_json(base_json_path, game_version):
        return False

    if not _is_valid_fabric_json(target_json_path, game_version, target_version):
        return False

    if not _sanitize_version_json(target_json_path):
        return False

    if not _sync_inherited_client_jar(mc_dir, game_version, target_version):
        return False

    try:
        with open(base_json_path, "r", encoding="utf-8") as f:
            base_data = json.load(f)
        asset_index_id = base_data.get("assetIndex", {}).get("id") or base_data.get("assets")
        if asset_index_id:
            asset_index_path = os.path.join(mc_dir, "assets", "indexes", f"{asset_index_id}.json")
            if not os.path.isfile(asset_index_path) or os.path.getsize(asset_index_path) <= 0:
                return False

        cmd = minecraft_launcher_lib.command.get_minecraft_command(
            target_version,
            mc_dir,
            {"username": "Player", "uuid": "00000000-0000-0000-0000-000000000000", "token": ""},
        )
        if "-cp" not in cmd:
            return False
        cp_arg = cmd[cmd.index("-cp") + 1]
        cp_entries = [entry for entry in cp_arg.split(os.pathsep) if entry]
        if not cp_entries:
            return False

        has_asm_util = False
        for entry in cp_entries:
            if not os.path.isfile(entry) or os.path.getsize(entry) <= 0:
                return False
            if entry.lower().endswith(".jar"):
                try:
                    with zipfile.ZipFile(entry, "r") as zf:
                        names = set(zf.namelist())
                        if not names:
                            return False
                        if "asm-util" in entry.lower() and "org/objectweb/asm/util/CheckClassAdapter.class" in names:
                            has_asm_util = True
                except Exception:
                    return False

        if not has_asm_util:
            return False
    except Exception:
        return False

    return True


def _resolve_cli_java(java_exe):
    if not java_exe:
        return shutil.which("java") or "java"
    if os.path.isfile(java_exe):
        if java_exe.lower().endswith("javaw.exe"):
            candidate = os.path.join(os.path.dirname(java_exe), "java.exe")
            if os.path.isfile(candidate):
                return candidate
        return java_exe
    resolved = shutil.which(java_exe)
    if resolved:
        if resolved.lower().endswith("javaw.exe"):
            candidate = os.path.join(os.path.dirname(resolved), "java.exe")
            if os.path.isfile(candidate):
                return candidate
        return resolved
    if java_exe.lower() in ("javaw", "javaw.exe"):
        return shutil.which("java") or "java"
    return java_exe


def _install_fabric_profile_and_libs(mc_dir, game_version, loader_version, target_version, callback_dict, cli_java):
    versions_dir = os.path.join(mc_dir, "versions", target_version)
    target_json_path = os.path.join(versions_dir, f"{target_version}.json")
    os.makedirs(versions_dir, exist_ok=True)
    _clean_tlauncher_profile_artifacts(versions_dir)

    if not _is_valid_fabric_json(target_json_path, game_version, target_version):
        if os.path.isfile(target_json_path):
            try:
                _clear_readonly(target_json_path)
                os.remove(target_json_path)
            except OSError:
                pass
        meta_url = f"https://meta.fabricmc.net/v2/versions/loader/{game_version}/{loader_version}/profile/json"
        fetched = False
        try:
            resp = requests.get(meta_url, headers={"user-agent": _mll_helper.get_user_agent()}, timeout=15)
            if resp.status_code == 200:
                profile_data = resp.json()
                with open(target_json_path, "w", encoding="utf-8") as f:
                    json.dump(profile_data, f, indent=2)
                fetched = _is_valid_fabric_json(target_json_path, game_version, target_version)
        except Exception:
            fetched = False

        if not fetched:
            try:
                minecraft_launcher_lib.fabric.install_fabric(
                    minecraft_version=game_version,
                    minecraft_directory=mc_dir,
                    loader_version=loader_version,
                    callback=callback_dict,
                    java=cli_java,
                )
            except Exception:
                pass

        if not _is_valid_fabric_json(target_json_path, game_version, target_version):
            if game_version == "26.3" and loader_version == "0.19.5":
                try:
                    with open(target_json_path, "w", encoding="utf-8") as f:
                        json.dump(PRISTINE_FABRIC_PROFILE_0_19_5_26_3, f, indent=2)
                except Exception:
                    pass

    _clean_tlauncher_profile_artifacts(versions_dir)
    _sanitize_version_json(target_json_path)

    if not os.path.isfile(target_json_path):
        raise RuntimeError(
            f"No se pudo crear ni descargar el perfil de Fabric ({target_version}.json). "
            "Comprobá tu conexión a internet y los permisos del sistema."
        )

    with open(target_json_path, "r", encoding="utf-8") as f:
        fabric_data = json.load(f)

    if isinstance(fabric_data.get("libraries"), list):
        minecraft_launcher_lib.install.install_libraries(
            target_version,
            fabric_data["libraries"],
            mc_dir,
            callback_dict,
        )
        _verify_and_repair_fabric_libraries(fabric_data["libraries"], mc_dir, callback_dict)


def ensure_minecraft_and_fabric(
    mc_dir,
    game_version="26.3",
    loader_version="0.19.5",
    java_exe=None,
    status_callback=None,
    progress_callback=None,
):
    """
    Verifica que la versión de Minecraft, sus librerías, el JAR del juego y Fabric Loader
    existan y estén íntegros. Si falta algún archivo o quedó corrupto, lo repara.
    """
    for value in (game_version, loader_version):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,99}", value) or ".." in value:
            raise ValueError("Identificador de versión inválido.")
    target_version = f"fabric-loader-{loader_version}-{game_version}"
    target_dir = os.path.join(mc_dir, "versions", target_version)
    target_json_path = os.path.join(target_dir, f"{target_version}.json")
    base_dir = os.path.join(mc_dir, "versions", game_version)
    base_json_path = os.path.join(base_dir, f"{game_version}.json")

    # Limpiar artefactos residuales y librerías legacy de TLauncher en mc_dir y versiones
    _clean_tlauncher_global_conflicts(mc_dir)
    _clean_tlauncher_profile_artifacts(target_dir)
    _clean_tlauncher_profile_artifacts(base_dir)

    if _is_fabric_installation_complete(mc_dir, game_version, target_version):
        return target_version

    if status_callback:
        status_callback(f"Instalando Fabric Loader {loader_version} para Minecraft {game_version}...")

    callback_dict = _make_mll_progress_callback(progress_callback)
    cli_java = _resolve_cli_java(java_exe)

    # Si 26.3.json fue modificado por otro launcher (ej. TLauncher sin sección downloads
    # o con argumentos incompatibles), eliminarlo para descargar el oficial de Mojang.
    if os.path.isfile(base_json_path) and not _is_valid_vanilla_json(base_json_path, game_version):
        try:
            _clear_readonly(base_json_path)
            os.remove(base_json_path)
        except OSError:
            pass

    # Si target_json_path existe pero es inválido o corrupto (ej. generado por TLauncher),
    # eliminarlo para que se descargue limpiamente el perfil oficial de Fabric
    if os.path.isfile(target_json_path) and not _is_valid_fabric_json(target_json_path, game_version, target_version):
        try:
            _clear_readonly(target_json_path)
            os.remove(target_json_path)
        except OSError:
            pass

    # 1. Asegurar que la versión base de Minecraft esté instalada e íntegra
    minecraft_launcher_lib.install.install_minecraft_version(
        game_version,
        mc_dir,
        callback=callback_dict,
    )

    # 2. Instalar el perfil y librerías de Fabric Loader sin borrar el JAR del cliente
    _install_fabric_profile_and_libs(
        mc_dir,
        game_version,
        loader_version,
        target_version,
        callback_dict,
        cli_java,
    )

    # 3. Asegurar que tanto 26.3.jar como fabric-loader-0.19.5-26.3.jar existan y sean válidos
    if not _ensure_client_jar_downloaded(mc_dir, game_version, target_version, callback_dict):
        raise RuntimeError(
            f"No se pudo verificar ni descargar el archivo principal de Minecraft {game_version} ({game_version}.jar)."
        )

    # 4. Re-verificar que la instalación completa de Fabric esté íntegra
    if not _is_fabric_installation_complete(mc_dir, game_version, target_version):
        raise RuntimeError(
            f"La instalación de Fabric Loader {loader_version} para Minecraft {game_version} "
            "quedó incompleta o faltan librerías. Por favor, reintentá iniciar la partida."
        )

    return target_version
