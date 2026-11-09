#!/usr/bin/env python3
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse, urlsplit

HOST, PORT = "127.0.0.1", 8765
ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
MONITOR = "TAB"
ADDR = re.compile(r"0x[0-9a-f]{1,16}")


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

def load_config():
    user = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "tabdeck" / "config.toml"
    path = user if user.is_file() else ROOT / "config.toml"
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    for i, k in enumerate(cfg.get("keys", [])):
        if "workspace" in k:
            if not isinstance(k["workspace"], int):
                sys.exit(f"{path}: keys[{i}].workspace must be a number")
        elif not isinstance(k.get("command"), (str, list)) or not k.get("label"):
            sys.exit(f"{path}: keys[{i}] needs either workspace, or label and command")
    return cfg


CFG = load_config()
HW = CFG.get("hardware", {})


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


# Only these fixed commands can run; the client sends an id, never a command.
ACTIONS = {
    "voldown": ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "5%-"],
    "mute": ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"],
    "volup": ["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "5%+"],
    "prev": ["playerctl", "previous"],
    "play": ["playerctl", "play-pause"],
    "next": ["playerctl", "next"],
}
KEYS = []
for i, k in enumerate(CFG.get("keys", [])):
    kid = f"k{i}"
    if "workspace" in k:
        ACTIONS[kid] = workspace_cmd(k["workspace"])
        KEYS.append({"id": kid, "ws": k["workspace"]})
    else:
        c = k["command"]
        ACTIONS[kid] = ["sh", "-c", c] if isinstance(c, str) else c
        KEYS.append({"id": kid, "label": k["label"], "icon": k.get("icon")})


# --- hardware detection ---

def detect_cpu_sensor():
    names = {read(h / "name"): h for h in Path("/sys/class/hwmon").iterdir()}
    want = HW.get("cpu_sensor", "auto")
    for name in ([want] if want != "auto" else ["k10temp", "zenpower", "coretemp", "cpu_thermal", "acpitz"]):
        if name in names:
            return names[name] / "temp1_input"
    return None


def detect_battery():
    want = HW.get("battery", "auto")
    if want != "auto":
        return Path("/sys/class/power_supply") / want
    for p in sorted(Path("/sys/class/power_supply").iterdir()):
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
        return {"windows": [], "workspaces": []}
    wins = sorted(
        ({"address": c["address"], "class": c["class"], "title": c["title"],
          "workspace": c["workspace"]["id"], "active": c["address"] == active}
         for c in clients if c.get("mapped", True) and c["workspace"]["id"] > 0),
        key=lambda w: (w["workspace"], w["class"]))
    return {"windows": wins, "workspaces": [m["activeWorkspace"]["id"] for m in monitors]}


# --- virtual display setup, used by the tablet script ---

def tablet_resolution():
    res = CFG.get("display", {}).get("resolution", "auto")
    if res != "auto":
        return res
    m = re.search(r"(\d+)x(\d+)", run(["adb", "-d", "shell", "wm", "size"], timeout=5))
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


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body=b"", ctype="text/plain", cache=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data):
        self._send(200, json.dumps(data).encode(), "application/json")

    def do_GET(self):
        path = urlsplit(self.path).path
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
        path = urlsplit(self.path).path
        verb, _, arg = path.strip("/").partition("/")
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


if __name__ == "__main__":
    if sys.argv[1:] == ["setup-display"]:
        setup_display()
    else:
        ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
