import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import tomllib
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
CONFIG_HOME = tempfile.TemporaryDirectory()
os.environ["XDG_CONFIG_HOME"] = CONFIG_HOME.name
sys.path.insert(0, str(ROOT))

import tabdeck  # noqa: E402

TOKEN = "test-token"
tabdeck.TOKEN = TOKEN
HOST = {"Host": "127.0.0.1:8765", "Cookie": f"tabdeck={TOKEN}"}
ORIGIN = {"Origin": "http://127.0.0.1:8765"}


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost")
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.path)


class StateMixin:
    def setUp(self):
        self.saved = (tabdeck.CFG, tabdeck.ACTIONS, tabdeck.KEYS, tabdeck.LUA)
        self.user_cfg = tabdeck.user_config_path()
        self.user_cfg.unlink(missing_ok=True)

    def tearDown(self):
        tabdeck.CFG, tabdeck.ACTIONS, tabdeck.KEYS, tabdeck.LUA = self.saved
        self.user_cfg.unlink(missing_ok=True)


class Shortcuts(unittest.TestCase):
    def test_modifiers_wrap_the_key(self):
        self.assertEqual(tabdeck.shortcut_cmd("ctrl+shift+t"),
                         ["wtype", "-M", "ctrl", "-M", "shift", "-k", "t", "-m", "shift", "-m", "ctrl"])

    def test_plain_key_and_super(self):
        self.assertEqual(tabdeck.shortcut_cmd("F5"), ["wtype", "-k", "F5"])
        self.assertEqual(tabdeck.shortcut_cmd("super+Return"), ["wtype", "-M", "logo", "-k", "Return", "-m", "logo"])

    def test_rejects_bad_specs(self):
        for bad in ["ctrl+;rm -rf", "hyper+a", "ctrl+", "", "a b"]:
            self.assertIsNone(tabdeck.SHORTCUT.fullmatch(bad), bad)


class HyprlandCommands(StateMixin, unittest.TestCase):
    def test_classic(self):
        tabdeck.LUA = False
        self.assertEqual(tabdeck.workspace_cmd(3), ["hyprctl", "dispatch", "workspace", "3"])
        self.assertEqual(tabdeck.window_cmd("focus", "0xab"), ["hyprctl", "dispatch", "focuswindow", "address:0xab"])
        self.assertEqual(tabdeck.window_cmd("close", "0xab"), ["hyprctl", "dispatch", "closewindow", "address:0xab"])

    def test_lua(self):
        tabdeck.LUA = True
        self.assertEqual(tabdeck.workspace_cmd(3)[2], 'hl.dispatch(hl.dsp.focus({workspace = "3"}))')
        self.assertEqual(tabdeck.window_cmd("close", "0xab")[2],
                         'hl.dispatch(hl.dsp.window.close({window = "address:0xab"}))')

    def test_window_address_pattern(self):
        self.assertTrue(tabdeck.ADDR.fullmatch("0x55897e5c0800"))
        for bad in ["0x1;rm", "abc", "0x", "0xZZ"]:
            self.assertIsNone(tabdeck.ADDR.fullmatch(bad), bad)


class Config(unittest.TestCase):
    def test_default_config_is_valid(self):
        with open(ROOT / "config.toml", "rb") as f:
            self.assertIsNone(tabdeck.config_error(tomllib.load(f)))

    def test_invalid_keys(self):
        cases = [
            {"keys": [{"workspace": "1"}]},
            {"keys": [{"label": "x"}]},
            {"keys": [{"label": "x", "shortcut": "ctrl+;"}]},
            {"apps": {"kitty": {"keys": [{"command": "ls"}]}}},
        ]
        for cfg in cases:
            self.assertIsNotNone(tabdeck.config_error(cfg), cfg)

    def test_display_and_port_settings(self):
        bad = [
            {"display": {"name": "TAB;rm"}},
            {"display": {"refresh": 0}},
            {"display": {"refresh": "60"}},
            {"ports": {"command_center": 80}},
            {"ports": {"display": "5900"}},
            {"ports": {"command_center": 5900}},
        ]
        for cfg in bad:
            self.assertIsNotNone(tabdeck.config_error(cfg), cfg)
        ok = {"display": {"name": "TABLET-2", "refresh": 120}, "ports": {"command_center": 9000, "display": 5901}}
        self.assertIsNone(tabdeck.config_error(ok))

    def test_toml_round_trip(self):
        with open(ROOT / "config.toml", "rb") as f:
            cfg = tomllib.load(f)
        cfg["apps"]["org.kde.dolphin"] = {"keys": [{"label": 'Say "hi" \\ ünïcode', "command": ["echo", "a b"]}]}
        cfg["edit"]["commands"] = True
        self.assertEqual(tomllib.loads(tabdeck.dump_toml(cfg)), cfg)

    def test_key_ids_map_to_lists(self):
        cfg = {"keys": [{"workspace": 1}], "apps": {"kitty": {"keys": [{"label": "a", "shortcut": "F5"}]}}}
        self.assertEqual(tabdeck.key_list(cfg, "k0"), (cfg["keys"], 0))
        self.assertEqual(tabdeck.key_list(cfg, "a0k0"), (cfg["apps"]["kitty"]["keys"], 0))
        for bad in ["k1", "a1k0", "x", "k-1", "a0k"]:
            self.assertEqual(tabdeck.key_list(cfg, bad), (None, None), bad)


class EditorInput(unittest.TestCase):
    off = {"edit": {"commands": False}}
    on = {"edit": {"commands": True}}

    def check_rejected(self, data, old=None, cfg=off):
        with self.assertRaises(tabdeck.EditError):
            tabdeck.key_from_client(data, old, cfg)

    def test_valid_keys(self):
        self.assertEqual(tabdeck.key_from_client({"type": "workspace", "value": "7"}, None, self.off), {"workspace": 7})
        self.assertEqual(
            tabdeck.key_from_client({"type": "shortcut", "label": " Tab ", "value": "ctrl+t", "icon": "plus"}, None, self.off),
            {"label": "Tab", "icon": "plus", "shortcut": "ctrl+t"})

    def test_rejects_bad_fields(self):
        self.check_rejected({"type": "workspace", "value": "0"})
        self.check_rejected({"type": "workspace", "value": "1; ls"})
        self.check_rejected({"type": "shortcut", "label": "", "value": "F5"})
        self.check_rejected({"type": "shortcut", "label": "x" * 25, "value": "F5"})
        self.check_rejected({"type": "shortcut", "label": "x", "value": "ctrl+;id"})
        self.check_rejected({"type": "shortcut", "label": "x", "value": "F5", "icon": "../../etc/passwd"})
        self.check_rejected({"type": "shortcut", "label": "x", "value": "F5", "icon": "no-such-icon"})
        self.check_rejected({"type": "bogus", "label": "x", "value": "F5"})

    def test_commands_locked_unless_enabled(self):
        self.check_rejected({"type": "command", "label": "x", "value": "touch /tmp/x"})
        old = {"label": "Shot", "command": ["hyprshot", "-m", "region"]}
        kept = tabdeck.key_from_client({"type": "command", "label": "Snap", "value": "hyprshot -m region"}, old, self.off)
        self.assertEqual(kept, {"label": "Snap", "command": ["hyprshot", "-m", "region"]})
        self.check_rejected({"type": "command", "label": "Snap", "value": "hyprshot -m output"}, old)
        new = tabdeck.key_from_client({"type": "command", "label": "x", "value": "kitty"}, None, self.on)
        self.assertEqual(new["command"], "kitty")


class Editing(StateMixin, unittest.TestCase):
    def test_save_insert_delete_persist(self):
        tabdeck.edit_key("k6", "save", {"type": "shortcut", "label": "Tab", "value": "ctrl+t", "icon": "plus"})
        self.assertEqual(tabdeck.ACTIONS["k6"], tabdeck.shortcut_cmd("ctrl+t"))
        tabdeck.edit_key("k0", "insert", {"type": "workspace", "value": "9"})
        saved = tomllib.loads(self.user_cfg.read_text())
        self.assertEqual(saved["keys"][1], {"workspace": 9})
        self.assertEqual(saved["keys"][7]["label"], "Tab")
        n = len(saved["keys"])
        tabdeck.edit_key("k1", "delete", {})
        self.assertEqual(len(tomllib.loads(self.user_cfg.read_text())["keys"]), n - 1)

    def test_move_within_page_only(self):
        first, second = tabdeck.CFG["keys"][6], tabdeck.CFG["keys"][7]
        tabdeck.edit_key("k7", "move", {"to": "k6"})
        self.assertEqual(tabdeck.CFG["keys"][6:8], [second, first])
        with self.assertRaises(tabdeck.EditError):
            tabdeck.edit_key("k6", "move", {"to": "a0k0"})

    def test_create_page_and_drop_it_when_empty(self):
        tabdeck.create_page("org.kde.Dolphin", {"type": "shortcut", "label": "Up", "value": "alt+Up"})
        self.assertEqual(tabdeck.KEYS["apps"]["org.kde.dolphin"][0]["label"], "Up")
        saved = tomllib.loads(self.user_cfg.read_text())
        self.assertIn("org.kde.dolphin", saved["apps"])
        with self.assertRaises(tabdeck.EditError):
            tabdeck.create_page("org.kde.Dolphin", {"type": "shortcut", "label": "Up", "value": "alt+Up"})
        kid = tabdeck.KEYS["apps"]["org.kde.dolphin"][0]["id"]
        tabdeck.edit_key(kid, "delete", {})
        self.assertNotIn("org.kde.dolphin", tabdeck.CFG["apps"])

    def test_create_page_rejects_bad_class(self):
        for bad in ["", "a b", "../x", "x" * 65, 'a"b']:
            with self.assertRaises(tabdeck.EditError):
                tabdeck.create_page(bad, {"type": "workspace", "value": "1"})

    def test_unknown_key_or_op(self):
        with self.assertRaises(tabdeck.EditError):
            tabdeck.edit_key("k999", "delete", {})
        with self.assertRaises(tabdeck.EditError):
            tabdeck.edit_key("k0", "explode", {})
        self.assertFalse(self.user_cfg.exists())


class Mull(StateMixin, unittest.TestCase):
    def fake_which(self, name):
        return f"/usr/bin/{name}"

    def test_status_never_contains_key_values(self):
        env = Path(CONFIG_HOME.name) / "mull" / ".env"
        env.parent.mkdir(parents=True, exist_ok=True)
        env.write_text('GROQ_API_KEY=gsk_supersecretvalue123\nOPENROUTER_API_KEY=""\n')
        with mock.patch.object(tabdeck.shutil, "which", self.fake_which), \
                mock.patch.object(tabdeck, "run", return_value="fix\nshorten\nbad preset!"):
            status = tabdeck.mull_status()
        self.assertNotIn("gsk_supersecretvalue123", json.dumps(status))
        self.assertEqual(status["backends"], {"groq": True, "openrouter": False, "claude": True})
        self.assertEqual(status["presets"], ["fix", "shorten"])

    def test_not_installed(self):
        with mock.patch.object(tabdeck.shutil, "which", return_value=None):
            self.assertEqual(tabdeck.mull_status(), {"enabled": True, "installed": False})

    def test_disabled_in_config(self):
        tabdeck.CFG = {**tabdeck.CFG, "integrations": {"mull": False}}
        self.assertEqual(tabdeck.mull_status(), {"enabled": False})

    def test_rewrite_validates_input(self):
        with mock.patch.object(tabdeck.shutil, "which", self.fake_which), \
                mock.patch.object(tabdeck, "run", return_value="fix"):
            for bad in [{}, {"preset": "rm -rf"}, {"preset": "nope"}, {"instruction": "x" * 301}, {"instruction": "  "}]:
                with self.assertRaises(tabdeck.EditError, msg=bad):
                    tabdeck.mull_rewrite(bad)


class Http(StateMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.sock = os.path.join(cls.dir.name, "http.sock")
        cls.server = tabdeck.UnixHTTPServer(cls.sock, tabdeck.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.dir.cleanup()

    def request(self, method, path, headers=None, body=None):
        conn = UnixConnection(self.sock)
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, data

    def test_page_and_keys(self):
        self.assertEqual(self.request("GET", "/", HOST)[0], 200)
        status, body = self.request("GET", "/api/keys", HOST)
        self.assertEqual(status, 200)
        self.assertIn("default", json.loads(body))

    def test_rejects_other_hosts_and_origins(self):
        self.assertEqual(self.request("GET", "/api/keys", {"Host": "evil.com"})[0], 403)
        self.assertEqual(self.request("GET", "/api/keys", {**HOST, "Origin": "https://evil.com"})[0], 403)
        self.assertEqual(self.request("POST", "/run/nope", HOST)[0], 403)
        self.assertEqual(self.request("POST", "/run/nope", {**HOST, "Origin": "https://evil.com"})[0], 403)

    def test_api_needs_the_cookie(self):
        no_cookie = {"Host": "127.0.0.1:8765"}
        self.assertEqual(self.request("GET", "/api/keys", no_cookie)[0], 401)
        self.assertEqual(self.request("GET", "/api/keys", {**no_cookie, "Cookie": "tabdeck=wrong"})[0], 401)
        self.assertEqual(self.request("POST", "/run/nope", {**no_cookie, **ORIGIN})[0], 401)
        self.assertEqual(self.request("GET", "/", no_cookie)[0], 200)

    def test_unlock_sets_cookie(self):
        conn = UnixConnection(self.sock)
        conn.request("GET", f"/?t={TOKEN}", headers={"Host": "127.0.0.1:8765"})
        r = conn.getresponse()
        r.read()
        conn.close()
        self.assertEqual(r.status, 303)
        cookie = r.getheader("Set-Cookie")
        self.assertIn(f"tabdeck={TOKEN}", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(self.request("GET", "/?t=wrong", {"Host": "127.0.0.1:8765"})[0], 200)

    def test_rejects_unknown_actions_and_paths(self):
        self.assertEqual(self.request("POST", "/run/nope", {**HOST, **ORIGIN})[0], 404)
        self.assertEqual(self.request("POST", "/focus/0x1;rm", {**HOST, **ORIGIN})[0], 400)
        self.assertEqual(self.request("GET", "/../tabdeck.py", HOST)[0], 404)
        self.assertEqual(self.request("GET", "/%2e%2e/tabdeck.py", HOST)[0], 404)

    def test_edit_over_http(self):
        h = {**HOST, **ORIGIN, "Content-Type": "application/json"}
        body = json.dumps({"op": "save", "key": {"type": "shortcut", "label": "Tab", "value": "ctrl+t"}})
        status, data = self.request("POST", "/keys/k6", h, body)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["default"][6]["label"], "Tab")
        evil = json.dumps({"op": "save", "key": {"type": "command", "label": "x", "value": "id"}})
        self.assertEqual(self.request("POST", "/keys/k6", h, evil)[0], 400)
        self.assertEqual(self.request("POST", "/keys/k6", {**HOST, **ORIGIN}, body)[0], 400)


if __name__ == "__main__":
    unittest.main()
