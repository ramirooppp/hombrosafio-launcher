import socket
import json
import re
import time

_MC_FORMAT_REGEX = re.compile(r"§[0-9a-fk-orA-FK-OR]")

def _strip_minecraft_formatting(text):
    if not text:
        return ""
    return _MC_FORMAT_REGEX.sub("", text)

def _extract_component_text(component):
    if isinstance(component, str):
        return component
    if isinstance(component, list):
        return "".join(_extract_component_text(c) for c in component)
    if isinstance(component, dict):
        text = component.get("text", "")
        extra = component.get("extra", [])
        if extra:
            return text + _extract_component_text(extra)
        return text
    return str(component) if component is not None else ""

def _varint(value):
    encoded = bytearray()
    while True:
        byte = value & 127
        value >>= 7
        encoded.append(byte | (128 if value else 0))
        if not value:
            return bytes(encoded)


def ping_server(host_port_str, timeout=3.0):
    """Bound both response size and total read time from an untrusted server."""
    try:
        host, separator, port_text = host_port_str.partition(":")
        port = int(port_text) if separator else 25565
        host_bytes = host.encode("utf-8")
        if not host or len(host_bytes) > 255 or not 1 <= port <= 65535:
            raise ValueError("Dirección de servidor inválida.")
        deadline = time.monotonic() + timeout
        with socket.create_connection((host, port), timeout=timeout) as sock:
            def read_exact(length):
                result = bytearray()
                while len(result) < length:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Tiempo de respuesta agotado.")
                    sock.settimeout(remaining)
                    chunk = sock.recv(length - len(result))
                    if not chunk:
                        raise ValueError("Respuesta incompleta.")
                    result.extend(chunk)
                return bytes(result)

            def read_varint():
                value = 0
                for shift in range(0, 35, 7):
                    byte = read_exact(1)[0]
                    value |= (byte & 127) << shift
                    if not byte & 128:
                        return value
                raise ValueError("VarInt inválido.")

            handshake = b"\x00\x00" + _varint(len(host_bytes)) + host_bytes + port.to_bytes(2, "big") + b"\x01"
            sock.sendall(_varint(len(handshake)) + handshake + b"\x01\x00")
            length = read_varint()
            if length < 2 or length > 1024 * 1024:
                raise ValueError("Respuesta del servidor demasiado grande.")
            if read_varint() != 0:
                raise ValueError("Tipo de respuesta inválido.")
            size = read_varint()
            if size > length - 1 or size > 1024 * 1024:
                raise ValueError("Tamaño JSON inválido.")
            data = json.loads(read_exact(size).decode("utf-8"))
        players = data.get("players", {})
        motd = _strip_minecraft_formatting(_extract_component_text(data.get("description", ""))).strip()
        return {"online": True, "players_online": players.get("online", 0),
                "players_max": players.get("max", 0), "motd": motd,
                "version": data.get("version", {}).get("name", "26.3")}
    except Exception as error:
        return {"online": False, "error": str(error)}
