import json
import os
import re
import shutil
import uuid
import subprocess
import zipfile
import minecraft_launcher_lib
from core.runtime_manager import (
    _is_valid_client_jar,
    _no_window_flags,
    _resolve_cli_java,
    _safe_download_file,
    clean_java_env,
)

# Safe patch for minecraft_launcher_lib bug with Fabric argument "values" vs "value"
if hasattr(minecraft_launcher_lib.command, "get_arguments"):
    _orig_get_arguments = minecraft_launcher_lib.command.get_arguments

    def _safe_get_arguments(data, versionData, path, options, classpath):
        if isinstance(data, list):
            copied_data = []
            for i in data:
                if isinstance(i, dict):
                    item_copy = dict(i)
                    if "value" not in item_copy and "values" in item_copy:
                        item_copy["value"] = item_copy["values"]
                    copied_data.append(item_copy)
                else:
                    copied_data.append(i)
            data = copied_data
        return _orig_get_arguments(data, versionData, path, options, classpath)

    minecraft_launcher_lib.command.get_arguments = _safe_get_arguments


def _parse_version_tuple(ver_str):
    nums = [int(x) for x in re.findall(r"\d+", str(ver_str))]
    return tuple(nums) if nums else (0,)


def _find_library_jar(mc_dir, group, artifact, required_class=None):
    """
    Busca en el repositorio de librerías locales de Minecraft la versión más reciente
    y válida de un artefacto (ej. org.ow2.asm:asm-util).
    Usa ordenamiento numérico por tuplas para no confundir '9.9' con mayor que '9.10.1'.
    """
    group_rel = group.replace(".", os.sep)
    artifact_dir = os.path.join(mc_dir, "libraries", group_rel, artifact)
    if not os.path.isdir(artifact_dir):
        return None
    try:
        versions = sorted(os.listdir(artifact_dir), key=_parse_version_tuple, reverse=True)
    except OSError:
        return None
    for ver in versions:
        jar_path = os.path.join(artifact_dir, ver, f"{artifact}-{ver}.jar")
        if os.path.isfile(jar_path) and os.path.getsize(jar_path) > 0:
            if required_class:
                try:
                    with zipfile.ZipFile(jar_path, "r") as zf:
                        if required_class in zf.namelist():
                            return jar_path
                except Exception:
                    continue
            else:
                return jar_path
    return None


def _parse_artifact_version(filename):
    """Extrae (nombre_artefacto, tupla_version) a partir de un nombre de archivo JAR."""
    if not filename.lower().endswith(".jar"):
        return None, None
    base = filename[:-4]
    parts = base.split("-")
    if len(parts) >= 2:
        ver_str = parts[-1]
        artifact_name = "-".join(parts[:-1]).lower()
        nums = [int(x) for x in re.findall(r"\d+", ver_str)]
        ver_tuple = tuple(nums) if nums else None
        return artifact_name, ver_tuple
    return base.lower(), None


REQUIRED_ASM_MODULES = [
    ("asm", "org/objectweb/asm/ClassReader.class", "ada2141c0cc52ee8f5c48cd5fa4ce0e794f22236"),
    ("asm-analysis", "org/objectweb/asm/tree/analysis/Analyzer.class", "8d49f14d51f632cb1d87c88d1ceaf50db0d8af1b"),
    ("asm-commons", "org/objectweb/asm/commons/ClassRemapper.class", "4229e4c55fd8e01c23f9fe9884075cc628aacc50"),
    ("asm-tree", "org/objectweb/asm/tree/ClassNode.class", "e244332a17564c1d1572449399a842de35881be2"),
    ("asm-util", "org/objectweb/asm/util/CheckClassAdapter.class", "7bb9d450e8d4cbf9f9e04096c44bbfe7fba80b15"),
]


def _sanitize_classpath_entries(entries, mc_dir):
    """
    Purga librerías legacy o conflictivas inyectadas por otros launchers como TLauncher
    (asm-all, asm-debug-all, tlloader) y asegura que asm-util y los módulos ASM
    necesarios para Fabric estén presentes en la versión correcta y verificados en disco.
    """
    filtered = []
    for p in entries:
        base_lower = os.path.basename(p).lower()
        if (
            base_lower.startswith("asm-all")
            or base_lower.startswith("asm-debug-all")
            or "tlauncher" in base_lower
            or "tlloader" in base_lower
        ):
            continue
        filtered.append(p)

    asm_modules = {"asm", "asm-analysis", "asm-commons", "asm-tree", "asm-util"}
    highest_asm_ver = {}
    for p in filtered:
        base = os.path.basename(p)
        art, ver = _parse_artifact_version(base)
        if art in asm_modules and ver is not None:
            if art not in highest_asm_ver or ver > highest_asm_ver[art]:
                highest_asm_ver[art] = ver

    deduped = []
    for p in filtered:
        base = os.path.basename(p)
        art, ver = _parse_artifact_version(base)
        if art in asm_modules and ver is not None and highest_asm_ver.get(art):
            if ver < highest_asm_ver[art]:
                continue
        deduped.append(p)

    verified_asm_jars = []
    for artifact, req_class, expected_sha1 in REQUIRED_ASM_MODULES:
        found_jar = None
        # Buscar si ya existe en deduped apuntando a un archivo real y válido
        for p in list(deduped):
            art, _ = _parse_artifact_version(os.path.basename(p))
            if art == artifact:
                if os.path.isfile(p) and os.path.getsize(p) > 0:
                    try:
                        with zipfile.ZipFile(p, "r") as zf:
                            if req_class in zf.namelist():
                                found_jar = p
                                break
                    except Exception:
                        pass
                # Si el archivo en deduped no existe o está corrupto, eliminarlo de deduped
                if not found_jar and p in deduped:
                    deduped.remove(p)

        if not found_jar:
            found_jar = _find_library_jar(mc_dir, "org.ow2.asm", artifact, required_class=req_class)

        if not found_jar:
            dl_dest = os.path.join(
                mc_dir, "libraries", "org", "ow2", "asm", artifact, "9.10.1", f"{artifact}-9.10.1.jar"
            )
            dl_url = f"https://maven.fabricmc.net/org/ow2/asm/{artifact}/9.10.1/{artifact}-9.10.1.jar"
            try:
                _safe_download_file(
                    dl_url,
                    dl_dest,
                    sha1=expected_sha1,
                    minecraft_directory=mc_dir,
                    overwrite=True,
                )
                if os.path.isfile(dl_dest) and os.path.getsize(dl_dest) > 0:
                    found_jar = dl_dest
            except Exception as e:
                print(f"[Classpath] Error descargando {artifact}: {e}")

        if found_jar:
            if found_jar not in deduped:
                deduped.append(found_jar)
            if found_jar not in verified_asm_jars:
                verified_asm_jars.append(found_jar)

    # Reordenar classpath: fabric-loader al inicio, seguido de los módulos ASM verificados, luego el resto
    non_asm = [p for p in deduped if p not in verified_asm_jars]
    loader_jars = [p for p in non_asm if "fabric-loader" in os.path.basename(p).lower()]
    other_jars = [p for p in non_asm if p not in loader_jars]
    return loader_jars + verified_asm_jars + other_jars


def _ensure_classpath_game_jar(cmd, version_id, mc_dir):
    if "-cp" not in cmd:
        return cmd
    cp_index = cmd.index("-cp") + 1
    if cp_index >= len(cmd):
        return cmd

    version_json = os.path.join(mc_dir, "versions", version_id, f"{version_id}.json")
    inherits_from = None
    if os.path.isfile(version_json):
        try:
            with open(version_json, "r", encoding="utf-8") as f:
                inherits_from = json.load(f).get("inheritsFrom")
        except Exception:
            inherits_from = None

    target_jar = os.path.join(mc_dir, "versions", version_id, f"{version_id}.jar")
    parent_jar = (
        os.path.join(mc_dir, "versions", inherits_from, f"{inherits_from}.jar")
        if inherits_from
        else None
    )

    if not _is_valid_client_jar(target_jar) and parent_jar and _is_valid_client_jar(parent_jar):
        try:
            os.makedirs(os.path.dirname(target_jar), exist_ok=True)
            shutil.copy2(parent_jar, target_jar)
        except OSError:
            pass

    valid_jar = None
    if _is_valid_client_jar(target_jar):
        valid_jar = target_jar
    elif parent_jar and _is_valid_client_jar(parent_jar):
        valid_jar = parent_jar

    cmd = list(cmd)
    raw_entries = [e for e in cmd[cp_index].split(os.pathsep) if e]
    entries = _sanitize_classpath_entries(raw_entries, mc_dir)

    if valid_jar:
        norm_target = os.path.normcase(os.path.abspath(target_jar))
        norm_valid = os.path.normcase(os.path.abspath(valid_jar))
        replaced = []
        has_valid = False
        for p in entries:
            norm_p = os.path.normcase(os.path.abspath(p))
            if norm_p == norm_target:
                replaced.append(valid_jar)
                has_valid = True
            elif norm_p == norm_valid:
                replaced.append(valid_jar)
                has_valid = True
            else:
                replaced.append(p)
        if not has_valid:
            replaced.append(valid_jar)
        cmd[cp_index] = os.pathsep.join(replaced)

        # Pass explicit Fabric system properties so Knot locates the verified client JAR directly
        fabric_props = [
            f"-Dfabric.gameJarPath={valid_jar}",
            f"-Dfabric.gameJarPath.client={valid_jar}",
        ]
        insert_pos = cmd.index("-cp")
        for prop in reversed(fabric_props):
            prop_key = prop.split("=", 1)[0] + "="
            if not any(arg.startswith(prop_key) for arg in cmd[:insert_pos]):
                cmd.insert(insert_pos, prop)
    else:
        cmd[cp_index] = os.pathsep.join(entries)

    # Strip any unexpanded template placeholders in JVM args that could break JVM startup
    cp_pos = cmd.index("-cp")
    cleaned_prefix = [cmd[0]]
    for arg in cmd[1:cp_pos]:
        if "${" in arg:
            continue
        cleaned_prefix.append(arg)
    cmd = cleaned_prefix + cmd[cp_pos:]

    return cmd


def _get_safe_max_heap_mb(requested_ram_gb):
    try:
        requested_mb = max(1024, int(requested_ram_gb) * 1024)
    except Exception:
        requested_mb = 4096

    if os.name != "nt":
        return requested_mb

    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            total_phys_mb = int(stat.ullTotalPhys // (1024 * 1024))
            # Base maximum heap on total physical RAM minus OS headroom;
            # _resolve_working_jvm_args performs preflight testing for OS acceptance.
            phys_cap_mb = max(1536, total_phys_mb - 1536)
            safe_mb = min(requested_mb, phys_cap_mb)
            safe_mb = max(1024, (safe_mb // 256) * 256)
            return safe_mb
    except Exception:
        pass

    return requested_mb


def _resolve_working_jvm_args(java_exe, requested_ram_gb):
    safe_mb = _get_safe_max_heap_mb(requested_ram_gb)

    base_flags = [
        "-Xms512M",
        "-XX:+UnlockExperimentalVMOptions",
        "-XX:+UseG1GC",
        "-XX:G1NewSizePercent=20",
        "-XX:G1ReservePercent=20",
        "-XX:MaxGCPauseMillis=50",
        "-XX:G1HeapRegionSize=32M",
    ]

    cli_java = _resolve_cli_java(java_exe or "java")
    candidates = [f"{safe_mb}M"]
    for fallback_mb in (4096, 3072, 2048, 1536, 1024):
        if fallback_mb < safe_mb:
            candidates.append(f"{fallback_mb}M")

    env = clean_java_env()
    for heap in candidates:
        test_args = [cli_java, f"-Xmx{heap}", *base_flags, "-version"]
        try:
            res = subprocess.run(
                test_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=8,
                env=env,
                creationflags=_no_window_flags(),
            )
            if res.returncode == 0:
                return [f"-Xmx{heap}", *base_flags]
        except Exception:
            continue

    return [f"-Xmx{safe_mb}M", *base_flags]


def _verify_full_jvm_prefix(cmd):
    if "-cp" not in cmd:
        return cmd
    cp_pos = cmd.index("-cp")
    cli_java = _resolve_cli_java(cmd[0])
    env = clean_java_env()

    try:
        res = subprocess.run(
            [cli_java, *cmd[1:cp_pos], "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=8,
            env=env,
            creationflags=_no_window_flags(),
        )
        if res.returncode == 0:
            return cmd
    except Exception:
        return cmd

    # If the full JVM prefix failed, try stepping down -Xmx
    for fallback_mb in (3072, 2048, 1536, 1024):
        adjusted_prefix = [
            f"-Xmx{fallback_mb}M" if arg.startswith("-Xmx") else arg
            for arg in cmd[1:cp_pos]
        ]
        try:
            res = subprocess.run(
                [cli_java, *adjusted_prefix, "-version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=8,
                env=env,
                creationflags=_no_window_flags(),
            )
            if res.returncode == 0:
                return [cmd[0], *adjusted_prefix, *cmd[cp_pos:]]
        except Exception:
            continue

    return cmd


def generate_offline_uuid(username):
    # UUID v3 basado en el nombre de usuario (estándar offline de Minecraft)
    return str(uuid.uuid3(uuid.NAMESPACE_DNS, f"OfflinePlayer:{username}"))


def launch_minecraft(
    version_id,
    mc_dir,
    game_dir,
    username,
    ram_gb=6,
    java_exe=None,
    server_ip=None,
    status_callback=None
):
    if not username:
        username = "Jugador"

    player_uuid = generate_offline_uuid(username)

    jvm_args = _resolve_working_jvm_args(java_exe, ram_gb)

    options = {
        "username": username,
        "uuid": player_uuid,
        "token": "",
        "gameDirectory": game_dir,
        "jvmArguments": jvm_args
    }

    if java_exe and os.path.isfile(java_exe):
        options["executablePath"] = java_exe

    if server_ip:
        if ":" in server_ip:
            h, p = server_ip.split(":", 1)
            options["server"] = h
            options["port"] = p
        else:
            options["server"] = server_ip

    if status_callback:
        status_callback("Construyendo argumentos de inicio...")

    cmd = minecraft_launcher_lib.command.get_minecraft_command(version_id, mc_dir, options)
    cmd = _ensure_classpath_game_jar(cmd, version_id, mc_dir)
    cmd = _verify_full_jvm_prefix(cmd)

    if status_callback:
        status_callback("¡Iniciando Minecraft!")

    # Redirigir stdout y stderr a launcher_game.log en la carpeta de juego
    log_dir = mc_dir if os.path.exists(mc_dir) else game_dir
    os.makedirs(log_dir, exist_ok=True)
    log_file_path = os.path.join(log_dir, "launcher_game.log")

    log_file = open(log_file_path, "w", encoding="utf-8", errors="replace")

    # Ejecutar en segundo plano de manera desacoplada y con entorno limpio de _JAVA_OPTIONS
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        cmd,
        cwd=game_dir,
        env=clean_java_env(),
        creationflags=creationflags,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    try:
        log_file.close()
    except Exception:
        pass

    proc.log_file_path = log_file_path
    return proc
