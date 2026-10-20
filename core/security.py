"""Validate remote metadata before it can choose a local write target."""
import os
import re
from pathlib import Path
from urllib.parse import urlparse

MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_MOD_BYTES = 512 * 1024 * 1024

def require_https(url):
    if not isinstance(url, str):
        raise ValueError("La URL debe ser texto.")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Las descargas requieren HTTPS sin credenciales en la URL.")
    return url

def mod_path(mods_dir, name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+() -]*\.jar", name, re.IGNORECASE):
        raise ValueError("Nombre de mod inválido.")
    if name.split('.')[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1,10)), *(f"LPT{i}" for i in range(1,10))}:
        raise ValueError("Nombre de archivo reservado por Windows.")
    root = Path(mods_dir).resolve()
    target = root / name
    if target.resolve().parent != root or target.is_symlink():
        raise ValueError("El archivo debe permanecer dentro de mods.")
    return str(target)

def validate_manifest(data):
    if not isinstance(data, dict) or not isinstance(data.get("mods"), list) or len(data["mods"]) > 512:
        raise ValueError("Formato de manifiesto inválido.")
    seen = set()
    for mod in data["mods"]:
        if not isinstance(mod, dict):
            raise ValueError("Entrada de mod inválida.")
        name = mod.get("name")
        mod_path(os.getcwd(), name)
        key = name.casefold()
        if key in seen:
            raise ValueError("El manifiesto repite un nombre de mod.")
        seen.add(key)
        require_https(mod.get("url"))
        if not isinstance(mod.get("sha1"), str) or not re.fullmatch(r"[0-9a-fA-F]{40}", mod["sha1"]):
            raise ValueError("Cada mod debe incluir su SHA-1.")
        size = mod.get("size", 0)
        if not isinstance(size, int) or isinstance(size, bool) or size < 0 or size > MAX_MOD_BYTES:
            raise ValueError("Tamaño de mod inválido.")
    return data
