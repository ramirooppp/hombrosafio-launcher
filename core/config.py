import os
import json

DEFAULT_MANIFEST_URL = "https://raw.githubusercontent.com/ramirooppp/hombrosafio/main/manifest.json"
DEFAULT_SERVER_IP = "67.222.149.6:25611"
OBSOLETE_SERVER_IPS = {"149.202.84.78:25601", "158.23.58.93", "158.23.58.93:25565"}

def get_base_hombrosafio_dir():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        appdata = os.path.expanduser("~")
    return os.path.join(appdata, ".hombrosafio")

def get_config_path():
    return os.path.join(get_base_hombrosafio_dir(), "launcher_config.json")

def load_config():
    config_path = get_config_path()
    default_config = {
        "username": "",
        "ram_gb": 6,
        "close_on_launch": True,
        "manifest_url": DEFAULT_MANIFEST_URL,
        "server_ip": DEFAULT_SERVER_IP
    }

    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
                default_config.update(data)
                # Auto-heal URLs obsoletas o que dan 404
                if "nahuel/hombrosafio-modpack" in default_config.get("manifest_url", ""):
                    default_config["manifest_url"] = DEFAULT_MANIFEST_URL
                # Auto-heal IP de servidor obsoleta
                if default_config.get("server_ip") in OBSOLETE_SERVER_IPS or not default_config.get("server_ip"):
                    default_config["server_ip"] = DEFAULT_SERVER_IP
        except Exception as e:
            print(f"[Config] Error leyendo config: {e}")

    return default_config

def save_config(config):
    hombrosafio_dir = get_base_hombrosafio_dir()
    os.makedirs(hombrosafio_dir, exist_ok=True)
    config_path = get_config_path()
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[Config] Error guardando config: {e}")
