#!/usr/bin/env python3
import hmac
import json
import mimetypes
import secrets
import os
import re
import shlex
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import tomllib
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse, urlsplit

PORT = 8765
ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
ICONS = STATIC / "icons"
RUNDIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "tabdeck"
SOCKET = RUNDIR / "http.sock"
MONITOR = "TAB"
ADDR = re.compile(r"0x[0-9a-f]{1,16}")
ICON_NAME = re.compile(r"[a-z0-9-]{1,64}")
APP_CLASS = re.compile(r"[A-Za-z0-9._-]{1,64}")
# The page is only ever loaded from the adb tunnel; anything else is another site trying to reach us.
ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


def run(cmd, timeout=2):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def read(path, default=""):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


# --- config ---

MODS = {"ctrl": "ctrl", "shift": "shift", "alt": "alt", "super": "logo", "logo": "logo"}
SHORTCUT = re.compile(r"((ctrl|shift|alt|super|logo)\+)*[A-Za-z0-9_]+", re.I)


def shortcut_cmd(spec):
    *mods, key = spec.split("+")
    mods = [MODS[m.lower()] for m in mods]
    cmd = ["wtype"]
    for m in mods:
        cmd += ["-M", m]
    cmd += ["-k", key]
    for m in reversed(mods):
        cmd += ["-m", m]
    return cmd


def user_config_path():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "tabdeck" / "config.toml"


def token_path():
    return user_config_path().parent / "token"


def load_token():
    """A per-machine secret the tablet's browser holds as a cookie; other tablet apps can't read it."""
    path = token_path()
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
    return path.read_text().strip()


def config_error(cfg):
    lists = {"keys": cfg.get("keys", [])}
    lists.update({f"apps.{a}.keys": v.get("keys", []) for a, v in cfg.get("apps", {}).items()})
    for where, keys in lists.items():
        for i, k in enumerate(keys):
            if "workspace" in k:
                if not isinstance(k["workspace"], int):
                    return f"{where}[{i}].workspace must be a number"
            elif "shortcut" in k:
                if not SHORTCUT.fullmatch(str(k["shortcut"])) or not k.get("label"):
                    return f'{where}[{i}] needs a label and a shortcut like "ctrl+shift+t"'
            elif not isinstance(k.get("command"), (str, list)) or not k.get("label"):
                return f"{where}[{i}] needs workspace, or label with command or shortcut"
    return None


def load_config():
    path = user_config_path() if user_config_path().is_file() else ROOT / "config.toml"
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    if err := config_error(cfg):
        sys.exit(f"{path}: {err}")
    return cfg


BARE = re.compile(r"[A-Za-z0-9_-]+")


def toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)


def toml_key(k):
    return k if BARE.fullmatch(k) else json.dumps(k)


def dump_toml(cfg):
    """Serializes tabdeck's config shape: scalars, flat tables, [[keys]] and [[apps.<name>.keys]]."""
    out = ["# Written by tabdeck. Comments are not kept when keys are edited from the tablet.", ""]
    for k, v in cfg.items():
        if not isinstance(v, (dict, list)) or (isinstance(v, list) and not any(isinstance(x, dict) for x in v)):
            out.append(f"{toml_key(k)} = {toml_value(v)}")
    for k, v in cfg.items():
        if isinstance(v, dict) and k != "apps":
            out += ["", f"[{toml_key(k)}]"] + [f"{toml_key(a)} = {toml_value(b)}" for a, b in v.items()]

    def tables(header, rows):
        for row in rows:
            out.extend(["", f"[[{header}]]"] + [f"{toml_key(a)} = {toml_value(b)}" for a, b in row.items()])

    tables("keys", cfg.get("keys", []))
    for name, app in cfg.get("apps", {}).items():
        tables(f"apps.{toml_key(name)}.keys", app.get("keys", []))
    return "\n".join(out) + "\n"


CFG = load_config()
HW = CFG.get("hardware", {})
CFG_LOCK = threading.Lock()


def hypr_lua():
    mode = CFG.get("hyprland", "auto")
    if mode == "auto":
        return (Path.home() / ".config/hypr/hyprland.lua").is_file()
    return mode == "lua"


LUA = hypr_lua()


def lua(expr):
    return ["hyprctl", "eval", f"hl.dispatch({expr})"]


def workspace_cmd(n):
    return lua(f'hl.dsp.focus({{workspace = "{n}"}})') if LUA else ["hyprctl", "dispatch", "workspace", str(n)]


def window_cmd(verb, addr):
    if LUA:
        dsp = "hl.dsp.focus" if verb == "focus" else "hl.dsp.window.close"
        return lua(f'{dsp}({{window = "address:{addr}"}})')
    return ["hyprctl", "dispatch", "focuswindow" if verb == "focus" else "closewindow", f"address:{addr}"]


# Only commands from the config can run; the client sends an id, never a command.
BASE_ACTIONS = {
    "voldown": ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "5%-"],
    "mute": ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"],
    "volup": ["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "5%+"],
    "prev": ["playerctl", "previous"],
    "play": ["playerctl", "play-pause"],
    "next": ["playerctl", "next"],
}


def command_text(c):
    return c if isinstance(c, str) else shlex.join(c)


def build_keys(keys, prefix, actions):
    out = []
    for i, k in enumerate(keys):
        kid = f"{prefix}{i}"
        if "workspace" in k:
            actions[kid] = workspace_cmd(k["workspace"])
            out.append({"id": kid, "ws": k["workspace"], "type": "workspace", "value": str(k["workspace"])})
            continue
        if "shortcut" in k:
            actions[kid] = shortcut_cmd(k["shortcut"])
            kind, value = "shortcut", k["shortcut"]
        else:
            c = k["command"]
            actions[kid] = ["sh", "-c", c] if isinstance(c, str) else c
            kind, value = "command", command_text(c)
        out.append({"id": kid, "label": k["label"], "icon": k.get("icon"), "type": kind, "value": value})
    return out


def build_all(cfg):
    actions = dict(BASE_ACTIONS)
    # Workspace keys stay on every page; an app page swaps out the other default keys.
    keys = {
        "default": build_keys(cfg.get("keys", []), "k", actions),
        "apps": {name.lower(): build_keys(app.get("keys", []), f"a{j}k", actions)
                 for j, (name, app) in enumerate(cfg.get("apps", {}).items())},
        "editCommands": bool(cfg.get("edit", {}).get("commands", False)),
    }
    return actions, keys


ACTIONS, KEYS = build_all(CFG)


def key_list(cfg, kid):
    """Maps a key id (k3, a1k0) to its list in the config and its index."""
    m = re.fullmatch(r"(?:a(\d+))?k(\d+)", kid)
    if not m:
        return None, None
    if m[1] is None:
        lst = cfg.setdefault("keys", [])
    else:
        apps = list(cfg.get("apps", {}).values())
        if int(m[1]) >= len(apps):
            return None, None
        lst = apps[int(m[1])].setdefault("keys", [])
    i = int(m[2])
    return (lst, i) if i < len(lst) else (None, None)


class EditError(Exception):
    pass


def key_from_client(data, old, cfg):
    """Builds a config key from untrusted editor input."""
    kind = data.get("type")
    value = str(data.get("value", "")).strip()
    if kind == "workspace":
        if not value.isdigit() or not 1 <= int(value) <= 99:
            raise EditError("Workspace must be a number from 1 to 99")
        return {"workspace": int(value)}
    label = str(data.get("label", "")).strip()
    if not 1 <= len(label) <= 24:
        raise EditError("Label must be 1 to 24 characters")
    key = {"label": label}
    icon = str(data.get("icon") or "")
    if icon:
        if not ICON_NAME.fullmatch(icon) or not (ICONS / f"{icon}.svg").is_file():
            raise EditError("Unknown icon")
        key["icon"] = icon
    if kind == "shortcut":
        if not SHORTCUT.fullmatch(value):
            raise EditError('Shortcut looks like "ctrl+shift+t"')
        key["shortcut"] = value
    elif kind == "command":
        unchanged = old is not None and "command" in old and command_text(old["command"]) == value
        if unchanged:
            key["command"] = old["command"]
        elif not cfg.get("edit", {}).get("commands", False):
            raise EditError("Editing commands from the tablet is off. Set edit.commands = true in the config")
        elif not 1 <= len(value) <= 500:
            raise EditError("Command must be 1 to 500 characters")
        else:
            key["command"] = value
    else:
        raise EditError("Unknown key type")
    return key


def edit_key(kid, op, data):
    """Applies one edit from the tablet, saves the user config and rebuilds the keys."""
    with CFG_LOCK:
        cfg = json.loads(json.dumps(CFG))
        lst, i = key_list(cfg, kid)
        if lst is None:
            raise EditError("Unknown key")
        if op == "delete":
            del lst[i]
            for name, app in list(cfg.get("apps", {}).items()):
                if not app.get("keys"):
                    del cfg["apps"][name]
        elif op == "move":
            to_lst, j = key_list(cfg, str(data.get("to", "")))
            if to_lst is not lst:
                raise EditError("Keys can only move within the same page")
            lst.insert(j, lst.pop(i))
        elif op == "save":
            lst[i] = key_from_client(data, lst[i], cfg)
        elif op == "insert":
            lst.insert(i + 1, key_from_client(data, None, cfg))
        else:
            raise EditError("Unknown operation")
        save_config(cfg)


def create_page(app_class, data):
    """Starts a key page for an app class with its first key."""
    if not APP_CLASS.fullmatch(app_class):
        raise EditError("Unsupported window class")
    name = app_class.lower()
    with CFG_LOCK:
        cfg = json.loads(json.dumps(CFG))
        apps = cfg.setdefault("apps", {})
        if apps.get(name, {}).get("keys"):
            raise EditError("This app already has a page")
        apps[name] = {"keys": [key_from_client(data, None, cfg)]}
        save_config(cfg)


def save_config(cfg):
    """Validates, writes the user config atomically and rebuilds keys. Caller holds CFG_LOCK."""
    global CFG, ACTIONS, KEYS
    if err := config_error(cfg):
        raise EditError(err)
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(dump_toml(cfg))
    tmp.replace(path)
    CFG = cfg
    ACTIONS, KEYS = build_all(cfg)


# --- hardware detection ---

def detect_cpu_sensor():
    names = {read(h / "name"): h for h in Path("/sys/class/hwmon").glob("hwmon*")}
    want = HW.get("cpu_sensor", "auto")
    for name in ([want] if want != "auto" else ["k10temp", "zenpower", "coretemp", "cpu_thermal", "acpitz"]):
        if name in names:
            return names[name] / "temp1_input"
    return None


def detect_battery():
    want = HW.get("battery", "auto")
    if want != "auto":
        return Path("/sys/class/power_supply") / want
    for p in sorted(Path("/sys/class/power_supply").glob("*")):
        if read(p / "type") == "Battery":
            return p
    return None


def detect_gpu():
    want = HW.get("gpu", "auto")
    if want in ("auto", "nvidia") and shutil.which("nvidia-smi"):
        for dev in Path("/sys/bus/pci/drivers/nvidia").glob("0000:*"):
            info = read(next(Path("/proc/driver/nvidia/gpus").glob(f"{dev.name}*/information"), "/dev/null"))
            model = next((l.split(":", 1)[1].strip() for l in info.splitlines() if l.startswith("Model")), "NVIDIA GPU")
            return {"kind": "nvidia", "dev": dev, "name": model.removeprefix("NVIDIA ").removeprefix("GeForce ")}
    if want in ("auto", "amd"):
        for busy in sorted(Path("/sys/class/drm").glob("card*/device/gpu_busy_percent")):
            return {"kind": "amd", "dev": busy.parent, "name": "AMD GPU"}
    return None


CPU_SENSOR = detect_cpu_sensor()
BATTERY = detect_battery()
GPU = detect_gpu()


def net_iface():
    if HW.get("net", "auto") != "auto":
        return HW["net"]
    for line in read("/proc/net/route").splitlines()[1:]:
        f = line.split()
        if f[1] == "00000000":
            return f[0]
    return None


# --- stats ---

prev = {"cpu": None, "net": None, "t": None}
gpu_cache = {"t": 0, "v": None}


def cpu_percent():
    fields = list(map(int, read("/proc/stat").splitlines()[0].split()[1:]))
    idle, total = fields[3] + fields[4], sum(fields)
    last, prev["cpu"] = prev["cpu"], (idle, total)
    if not last or total == last[1]:
        return 0
    return round(100 * (1 - (idle - last[0]) / (total - last[1])))


def net_rates(now):
    iface = net_iface()
    for line in read("/proc/net/dev").splitlines():
        if iface and line.strip().startswith(iface + ":"):
            v = line.split(":")[1].split()
            cur = (iface, int(v[0]), int(v[8]))
            break
    else:
        return 0, 0
    last, lt = prev["net"], prev["t"]
    prev["net"], prev["t"] = cur, now
    if not last or last[0] != iface or now == lt:
        return 0, 0
    return round((cur[1] - last[1]) / (now - lt)), round((cur[2] - last[2]) / (now - lt))


def gpu():
    if not GPU:
        return None
    # Querying a suspended dGPU wakes it and costs battery, so only ask when it is already awake.
    if read(GPU["dev"] / "power/runtime_status") == "suspended":
        return {"asleep": True, "name": GPU["name"]}
    if time.time() - gpu_cache["t"] < 5:
        return gpu_cache["v"]
    v = None
    try:
        if GPU["kind"] == "nvidia":
            out = run(["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total",
                       "--format=csv,noheader,nounits"])
            u, t, mu, mt = map(int, out.splitlines()[0].split(", "))
        else:
            d = GPU["dev"]
            u = int(read(d / "gpu_busy_percent"))
            temp = next(d.glob("hwmon/hwmon*/temp1_input"), None)
            t = round(int(read(temp)) / 1000) if temp else None
            mu, mt = (int(read(d / f"mem_info_vram_{x}", "0")) // 1048576 for x in ("used", "total"))
        v = {"asleep": False, "name": GPU["name"], "util": u, "temp": t, "memUsed": mu, "memTotal": mt}
    except (ValueError, IndexError):
        pass
    gpu_cache.update(t=time.time(), v=v)
    return v


def stats():
    now = time.monotonic()
    mem = {l.split(":")[0]: int(l.split()[1]) for l in read("/proc/meminfo").splitlines()}
    down, up = net_rates(now)
    t = read(CPU_SENSOR) if CPU_SENSOR else ""
    return {
        "host": socket.gethostname(),
        "uptime": int(float(read("/proc/uptime", "0").split()[0])),
        "cpu": cpu_percent(),
        "cpuTemp": round(int(t) / 1000) if t else None,
        "memUsed": mem["MemTotal"] - mem["MemAvailable"],
        "memTotal": mem["MemTotal"],
        "gpu": gpu(),
        "battery": {"level": int(read(BATTERY / "capacity", "0")), "status": read(BATTERY / "status")} if BATTERY else None,
        "net": {"down": down, "up": up},
    }


SEP = "\x1f"


def player():
    fmt = SEP.join(["{{status}}", "{{title}}", "{{artist}}", "{{album}}", "{{mpris:length}}",
                    "{{position}}", "{{mpris:artUrl}}", "{{playerName}}"])
    out = run(["playerctl", "metadata", "--format", fmt])
    vol = run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"])
    volume = {"level": round(float(vol.split()[1]) * 100) if vol else 0, "muted": "MUTED" in vol}
    if not out:
        return {"status": "Stopped", "volume": volume}
    status, title, artist, album, length, pos, art, name = (out.split(SEP) + [""] * 8)[:8]
    art_src = None
    if art.startswith(("http://", "https://")):
        art_src = art
    elif art.startswith("file://"):
        art_src = "/api/art?v=" + str(abs(hash(art)))
    return {"status": status, "title": title, "artist": artist, "album": album,
            "length": int(length or 0) / 1e6, "position": int(pos or 0) / 1e6,
            "art": art_src, "player": name.split(".")[0], "volume": volume}


def art_path():
    url = run(["playerctl", "metadata", "mpris:artUrl"])
    return Path(unquote(urlparse(url).path)) if url.startswith("file://") else None


def windows():
    try:
        clients = json.loads(run(["hyprctl", "clients", "-j"]) or "[]")
        active = json.loads(run(["hyprctl", "activewindow", "-j"]) or "{}").get("address")
        monitors = json.loads(run(["hyprctl", "monitors", "-j"]) or "[]")
    except json.JSONDecodeError:
        return {"windows": [], "workspaces": [], "activeClass": None}
    wins = sorted(
        ({"address": c["address"], "class": c["class"], "title": c["title"],
          "workspace": c["workspace"]["id"], "active": c["address"] == active}
         for c in clients if c.get("mapped", True) and c["workspace"]["id"] > 0),
        key=lambda w: (w["workspace"], w["class"]))
    active_class = next((w["class"] for w in wins if w["active"]), None)
    return {"windows": wins, "workspaces": [m["activeWorkspace"]["id"] for m in monitors], "activeClass": active_class}


# --- virtual display setup, used by the tablet script ---

def tablet_resolution():
    res = CFG.get("display", {}).get("resolution", "auto")
    if res != "auto":
        return res
    m = re.search(r"(\d+)x(\d+)", run(["adb", "shell", "wm", "size"], timeout=5))
    if not m:
        sys.exit("Could not read the tablet resolution; set display.resolution in the config")
    w, h = sorted(map(int, m.groups()), reverse=True)
    return f"{w}x{h}"


def setup_display():
    d = CFG.get("display", {})
    monitors = json.loads(run(["hyprctl", "monitors", "-j"]) or "[]")
    if not any(m["name"] == MONITOR for m in monitors):
        run(["hyprctl", "output", "create", "headless", MONITOR])
        time.sleep(1)
    mode, pos, scale = f"{tablet_resolution()}@60", d.get("position", "auto-right"), d.get("scale", 1)
    if LUA:
        run(["hyprctl", "eval", f'hl.monitor({{output="{MONITOR}", mode="{mode}", position="{pos}", scale={scale}}})'])
    else:
        run(["hyprctl", "keyword", "monitor", f"{MONITOR},{mode},{pos},{scale}"])
    tool = shutil.which("awww") or shutil.which("swww")
    if d.get("wallpaper", True) and tool:
        others = [m["name"] for m in monitors if m["name"] != MONITOR]
        for line in run([tool, "query"]).splitlines():
            if others and others[0] in line and "image: " in line:
                run([tool, "img", "-o", MONITOR, line.split("image: ", 1)[1]], timeout=10)
                break
    print(f"{MONITOR}: {mode} at {pos}")


TOKEN = None


class Handler(BaseHTTPRequestHandler):
    def _authed(self):
        try:
            c = SimpleCookie(self.headers.get("Cookie", "")).get("tabdeck")
        except CookieError:
            return False
        return bool(c and TOKEN and hmac.compare_digest(c.value, TOKEN))

    def _trusted(self, write):
        if self.headers.get("Host") not in HOSTS:
            return False
        origin = self.headers.get("Origin")
        return origin in ORIGINS if write else origin in (None, *ORIGINS)

    def _send(self, code, body=b"", ctype="text/plain", cache=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data):
        self._send(200, json.dumps(data).encode(), "application/json")

    def _unlock(self, given):
        if not (TOKEN and hmac.compare_digest(given, TOKEN)):
            return False
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", f"tabdeck={TOKEN}; Path=/; Max-Age=34560000; HttpOnly; SameSite=Strict")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    def do_GET(self):
        if not self._trusted(write=False):
            return self._send(403)
        url = urlsplit(self.path)
        path = url.path
        if path == "/" and (t := parse_qs(url.query).get("t")) and self._unlock(t[0]):
            return
        if path.startswith("/api/") and not self._authed():
            return self._send(401)
        if path == "/api/icons":
            return self._json(sorted(p.stem for p in ICONS.glob("*.svg")))
        if path == "/api/stats":
            return self._json(stats())
        if path == "/api/player":
            return self._json(player())
        if path == "/api/windows":
            return self._json(windows())
        if path == "/api/keys":
            return self._json(KEYS)
        if path == "/api/art":
            p = art_path()
            if p and p.is_file():
                return self._send(200, p.read_bytes(), mimetypes.guess_type(p)[0] or "image/png", cache=True)
            return self._send(404)
        file = (STATIC / (path.lstrip("/") or "index.html")).resolve()
        if file.is_file() and file.is_relative_to(STATIC.resolve()):
            ctype = mimetypes.guess_type(file)[0] or "application/octet-stream"
            return self._send(200, file.read_bytes(), ctype, cache=file.suffix in (".woff2", ".svg"))
        self._send(404)

    def do_POST(self):
        if not self._trusted(write=True):
            return self._send(403)
        if not self._authed():
            return self._send(401)
        path = urlsplit(self.path).path
        verb, _, arg = path.strip("/").partition("/")
        if verb in ("keys", "pages"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                if self.headers.get("Content-Type") != "application/json" or not 0 < length <= 4096:
                    raise EditError("Expected a small JSON body")
                body = json.loads(self.rfile.read(length))
                if verb == "pages":
                    create_page(arg, body.get("key") or {})
                else:
                    edit_key(arg, body.get("op"), body.get("key") or {})
            except EditError as e:
                return self._send(400, str(e).encode())
            except (ValueError, AttributeError):
                return self._send(400, b"Invalid request")
            return self._json(KEYS)
        if verb in ("focus", "close"):
            if not ADDR.fullmatch(arg):
                return self._send(400)
            cmd = window_cmd(verb, arg)
        elif verb == "run" and arg in ACTIONS:
            cmd = ACTIONS[arg]
        else:
            return self._send(404)
        try:
            subprocess.Popen(cmd, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            return self._send(500, str(e).encode())
        self._send(204)

    def log_message(self, *args):
        pass


class UnixHTTPServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def serve():
    global TOKEN
    TOKEN = load_token()
    # A socket in a 0700 runtime dir: only this user (and the adb tunnel it owns) can connect.
    RUNDIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    RUNDIR.chmod(0o700)
    SOCKET.unlink(missing_ok=True)
    UnixHTTPServer(str(SOCKET), Handler).serve_forever()


if __name__ == "__main__":
    if sys.argv[1:] == ["setup-display"]:
        setup_display()
    else:
        serve()
