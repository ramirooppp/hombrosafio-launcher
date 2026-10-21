import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from core.security import validate_manifest
from core.sync import fetch_manifest, sync_mods
from core.server_pinger import ping_server


class SecurityChecks(unittest.TestCase):
    def test_hovy_address_migrates_but_custom_address_is_preserved(self):
        from core import config
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory,"launcher_config.json")
            for old in ("67.222.149.6","67.222.149.6:25611","158.23.58.93"):
                path.write_text(json.dumps({"server_ip":old,"username":"TestPlayer"}))
                with patch.object(config,"get_config_path",return_value=str(path)):
                    result=config.load_config()
                    self.assertEqual(result["server_ip"],"158.23.58.93:25565")
                    self.assertEqual(result["username"],"TestPlayer")
            path.write_text(json.dumps({"server_ip":"custom.example:25565"}))
            with patch.object(config,"get_config_path",return_value=str(path)):
                self.assertEqual(config.load_config()["server_ip"],"custom.example:25565")

    def manifest(self, name="example.jar", body=b"verified mod"):
        return {"mods": [{"name": name, "url": "https://example.com/example.jar",
                          "sha1": hashlib.sha1(body).hexdigest(), "size": len(body)}]}

    def response(self, body, url="https://example.com/example.jar"):
        result = Mock(url=url)
        result.iter_content.return_value = [body]
        return result

    def test_reject_escape_ads_and_windows_devices_before_network_or_deletion(self):
        for name in ("../escape.jar", r"..\escape.jar", "/tmp/escape.jar", r"C:\escape.jar", "x:payload.jar", "CON.jar", "LPT1.jar"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                mods = Path(directory, "mods"); mods.mkdir()
                old = mods / "hombrosafio-old.jar"; old.write_bytes(b"keep")
                with patch("core.sync.requests.get") as get:
                    with self.assertRaises(ValueError): sync_mods(self.manifest(name), directory)
                    get.assert_not_called()
                self.assertEqual(old.read_bytes(), b"keep")

    def test_https_hash_and_case_insensitive_duplicates_required(self):
        for changes in ({"url": "http://example.com/a.jar"}, {"sha1": ""}, {"size": 2**40}):
            data = self.manifest(); data["mods"][0].update(changes)
            with self.assertRaises(ValueError): validate_manifest(data)
        data = self.manifest(); data["mods"].append(dict(data["mods"][0], name="EXAMPLE.JAR"))
        with self.assertRaises(ValueError): validate_manifest(data)

    def test_hash_mismatch_keeps_old_mod_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            mods = Path(directory, "mods"); mods.mkdir()
            old = mods / "hombrosafio-old.jar"; old.write_bytes(b"old")
            with patch("core.sync.requests.get", return_value=self.response(b"wrong")), patch("core.sync.time.sleep"):
                with self.assertRaises(RuntimeError): sync_mods(self.manifest(), directory)
            self.assertEqual(old.read_bytes(), b"old")
            self.assertEqual(sorted(p.name for p in mods.iterdir()), [old.name])

    def test_valid_download_is_verified_then_old_managed_mod_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            mods = Path(directory, "mods"); mods.mkdir(); (mods / "hombrosafio-old.jar").write_bytes(b"old")
            with patch("core.sync.requests.get", return_value=self.response(b"verified mod")):
                sync_mods(self.manifest(), directory)
            self.assertEqual((mods / "example.jar").read_bytes(), b"verified mod")
            self.assertFalse((mods / "hombrosafio-old.jar").exists())

    def test_invalid_remote_and_invalid_cache_are_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            bad = self.manifest("../escape.jar")
            Path(directory, "cached_manifest.json").write_text(json.dumps(bad))
            with patch("core.sync.requests.get", return_value=self.response(json.dumps(bad).encode())):
                self.assertIsNone(fetch_manifest("https://example.com/manifest.json", directory)[0])

    def test_ping_rejects_oversized_and_unterminated_varints_and_closes_socket(self):
        for packet in (b"\xff" * 5, b"\xff\xff\xff\x7f"):
            sock = Mock(); sock.recv.side_effect = [bytes([b]) for b in packet]
            context = Mock(); context.__enter__ = Mock(return_value=sock); context.__exit__ = Mock()
            with patch("core.server_pinger.socket.create_connection", return_value=context):
                self.assertFalse(ping_server("example.com")["online"])
            context.__exit__.assert_called_once()


if __name__ == "__main__": unittest.main()
