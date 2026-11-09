# tabdeck

Turn an Android tablet into a second monitor and a touch command center for a Hyprland desktop, over a USB cable.

- **Extended display.** A virtual Hyprland monitor streamed with wayvnc and shown in AVNC on the tablet.
- **Command center.** A fullscreen web app on the tablet with:
  - CPU history chart and temperature
  - memory, GPU, battery and network stats
  - now playing with album art, transport and volume controls
  - workspace keys with live indicators
  - app launcher keys
  - a window list where you can tap to focus a window or close it

Everything runs on your own PC and reaches the tablet through `adb reverse`. There's no cloud, no account and no app store install.

> **Status: early.** It's built for and tested on a single setup: Arch, Hyprland with the Lua config, HyDE, an NVIDIA laptop GPU and a Samsung Galaxy Tab A9. Several values are still hardcoded, as listed under [Limitations](#limitations).

## Requirements

**PC**
- Hyprland using the Lua config (`hyprctl eval`)
- `python3` (standard library only)
- `adb`, `wayvnc`
- `wpctl` (PipeWire), `playerctl`
- Optional: `nvidia-smi`, `awww` (wallpaper), HyDE (`hyde-shell`), `hyprshot`, `kitty`, `firefox`, `dolphin`, `rofi`

**Tablet**
- Android with USB debugging enabled
- [AVNC](https://github.com/gujjwal00/avnc) for the display
- A Chromium-based browser to install the command center as an app

## Setup

```sh
git clone https://github.com/silky-x0/tabdeck ~/tabdeck
ln -s ~/tabdeck/tablet ~/.local/bin/tablet
```

1. Plug in the tablet and accept the USB debugging prompt.
2. Run `tablet`.
3. On the tablet, open `http://127.0.0.1:8765` and choose *Install app* from the browser menu.

Every time after that, plug in the tablet, run `tablet` and open the app. To tear everything down, run `tablet stop`.

## How it works

| Piece | What it does |
| --- | --- |
| `tablet` | Creates a headless Hyprland output, starts `wayvnc` on it, starts the server and sets up `adb reverse` for ports 5900 and 8765 |
| `tabdeck.py` | Standard-library HTTP server. It serves the page, a JSON API with stats, player and window data, and a fixed list of actions |
| `static/` | The command center page, its fonts, icons and web app manifest |

## Security

The server has no authentication. It only listens on `127.0.0.1`, and the tablet reaches it through the USB tunnel. Any local user or process on the PC can call it.

The client can't run arbitrary commands:
- actions are a fixed list on the server, and the client only sends an id
- window addresses are checked against a strict pattern
- static files can't be served from outside `static/`

Don't bind it to `0.0.0.0` without adding authentication.

## Limitations

Hardcoded for now. Making these configurable is the plan for v1:

- GPU PCI path `0000:01:00.0` and `nvidia-smi`
- the `wlan0` network interface, `BAT1` battery and `k10temp` CPU sensor
- virtual monitor size `1340x800` and position `2646x0`
- the wallpaper copied from `eDP-1` with `awww`
- launcher apps: `kitty`, `firefox`, `dolphin`, and `hyde-shell rofilaunch`
- only the Lua `hyprctl eval` syntax, not classic `hyprctl dispatch`

VNC sends a single pointer, so touch on the display works as mouse input. Multi-touch gestures don't reach Linux apps.

## Roadmap

- Config file for keys, apps and hardware, with auto-detection
- Classic Hyprland config support
- Start automatically when the tablet is plugged in (udev + systemd user service)
- Per-app key pages that follow the focused window

## License

MIT, see [LICENSE](LICENSE). Bundled third-party assets are listed in [NOTICE](NOTICE).
