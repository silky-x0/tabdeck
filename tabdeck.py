#!/usr/bin/env python3
import json
import mimetypes
import re
import socket
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse, urlsplit

HOST, PORT = "127.0.0.1", 8765
STATIC = Path(__file__).parent / "static"
GPU_PCI = Path("/sys/bus/pci/devices/0000:01:00.0/power/runtime_status")
NET_IF = "wlan0"


def ws(n):
    return ["hyprctl", "eval", f'hl.dispatch(hl.dsp.focus({{workspace = "{n}"}}))']


# Only these fixed commands can run; the client sends an id, never a command.
ACTIONS = {
    **{f"ws{n}": ws(n) for n in range(1, 7)},
    "voldown": ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "5%-"],
    "mute": ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"],
    "volup": ["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "5%+"],
    "prev": ["playerctl", "previous"],
    "play": ["playerctl", "play-pause"],
    "next": ["playerctl", "next"],
    "term": ["kitty"],
    "web": ["firefox"],
    "files": ["dolphin"],
    "launcher": ["sh", "-c", "pkill -x rofi || exec hyde-shell rofilaunch d"],
    "shot": ["hyprshot", "-m", "region"],
    "lock": ["loginctl", "lock-session"],
}

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


def hwmon_temp(name):
    for h in Path("/sys/class/hwmon").iterdir():
        if read(h / "name") == name:
            t = read(h / "temp1_input")
            return round(int(t) / 1000) if t else None
    return None


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
    for line in read("/proc/net/dev").splitlines():
        if line.strip().startswith(NET_IF + ":"):
            v = line.split(":")[1].split()
            cur = (int(v[0]), int(v[8]))
            break
    else:
        return 0, 0
    last, lt = prev["net"], prev["t"]
    prev["net"], prev["t"] = cur, now
    if not last or now == lt:
        return 0, 0
    return round((cur[0] - last[0]) / (now - lt)), round((cur[1] - last[1]) / (now - lt))


def gpu():
    # Querying a suspended dGPU wakes it and costs battery, so only ask when it is already awake.
    if read(GPU_PCI) == "suspended":
        return {"asleep": True}
    if time.time() - gpu_cache["t"] > 5:
        out = run(["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total",
                   "--format=csv,noheader,nounits"])
        try:
            u, t, mu, mt = map(int, out.split(", "))
            gpu_cache["v"] = {"asleep": False, "util": u, "temp": t, "memUsed": mu, "memTotal": mt}
        except ValueError:
            gpu_cache["v"] = None
        gpu_cache["t"] = time.time()
    return gpu_cache["v"]


def stats():
    now = time.monotonic()
    mem = {l.split(":")[0]: int(l.split()[1]) for l in read("/proc/meminfo").splitlines()}
    down, up = net_rates(now)
    return {
        "host": socket.gethostname(),
        "uptime": int(float(read("/proc/uptime", "0").split()[0])),
        "cpu": cpu_percent(),
        "cpuTemp": hwmon_temp("k10temp"),
        "memUsed": mem["MemTotal"] - mem["MemAvailable"],
        "memTotal": mem["MemTotal"],
        "gpu": gpu(),
        "battery": {"level": int(read("/sys/class/power_supply/BAT1/capacity", "0")),
                    "status": read("/sys/class/power_supply/BAT1/status")},
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
        verb, _, addr = path.strip("/").partition("/")
        if verb in ("focus", "close"):
            if not ADDR.fullmatch(addr):
                return self._send(400)
            dsp = "hl.dsp.focus" if verb == "focus" else "hl.dsp.window.close"
            cmd = ["hyprctl", "eval", f'hl.dispatch({dsp}({{window = "address:{addr}"}}))']
        else:
            cmd = ACTIONS.get(path.removeprefix("/run/"))
            if not cmd:
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
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
