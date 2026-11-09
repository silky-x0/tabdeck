# tabdeck

Turn an Android tablet into a second monitor and a touch command center for a Hyprland desktop, over a USB cable.

![tabdeck on a Samsung Galaxy Tab A9: the key page follows the focused app, with live stats, now playing and the window list](docs/demo.gif)

- **Extended display.** A virtual Hyprland monitor streamed with wayvnc and shown in AVNC on the tablet.
- **Command center.** A fullscreen web app on the tablet with:
  - CPU history chart and temperature
  - memory, GPU, battery and network stats
  - now playing with album art, transport and volume controls
  - workspace keys with live indicators
  - app launcher keys, and per-app key pages that follow the focused window
  - a window list where you can tap to focus a window or close it

Everything runs on your own PC and reaches the tablet through `adb reverse`. There's no cloud, no account and no app store install.

> **Status: early.** It's tested on one setup: Arch, Hyprland with the Lua config, HyDE, an NVIDIA laptop GPU and a Samsung Galaxy Tab A9. Hardware is auto-detected, and keys and apps live in a config file. Classic Hyprland configs and AMD GPUs are supported in code but not yet tested on real machines.

## Requirements

**PC**
- Hyprland, with either the Lua config or the classic `hyprland.conf`
- `python3` (standard library only)
- `adb`, `wayvnc`
- `wpctl` (PipeWire), `playerctl`
- Optional: `wtype` (shortcut keys), `nvidia-smi` (NVIDIA stats), `awww` or `swww` (wallpaper copy), and whatever apps your keys launch. The default keys use `kitty`, `firefox`, `rofi` (HyDE's themed launcher when installed) and `hyprshot`

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

### Start automatically on plug-in

```sh
tablet install
```

This needs the tablet plugged in, and asks for sudo once. It installs:
- a udev rule that matches your tablet by serial number
- a systemd user service that runs `tablet` when the tablet is plugged in and tears everything down when it's unplugged

The service also starts at login if the tablet is already connected. To remove both, run `tablet uninstall`. Logs are in `journalctl --user -u tabdeck`.

## How it works

| Piece | What it does |
| --- | --- |
| `tablet` | Creates a headless Hyprland output sized to the tablet, starts `wayvnc` on it, starts the server and sets up `adb reverse` for ports 5900 and 8765 |
| `config.toml` | Keys, display placement, and hardware overrides |
| `tablet install` | Writes `/etc/udev/rules.d/70-tabdeck.rules` and `~/.config/systemd/user/tabdeck.service` |
| `tabdeck.py` | Standard-library HTTP server. It serves the page, a JSON API with stats, player and window data, and a fixed list of actions |
| `static/` | The command center page, its fonts, icons and web app manifest |

## Configuration

Copy the default config and edit it:

```sh
mkdir -p ~/.config/tabdeck
cp ~/tabdeck/config.toml ~/.config/tabdeck/config.toml
```

Your copy replaces the default entirely. Restart with `tablet stop && tablet` after changes.

**Keys** are shown 6 per row, in order. Each key is either a workspace switch or a command:

```toml
[[keys]]
workspace = 1

[[keys]]
label = "Terminal"
icon = "terminal"            # a file name from static/icons (Lucide)
command = "kitty"            # a string runs with sh -c

[[keys]]
label = "Capture"
icon = "scan"
command = ["hyprshot", "-m", "region"]   # a list runs directly, no shell
```

A key can also send a keyboard shortcut to the focused window. This needs `wtype`:

```toml
[[keys]]
label = "New tab"
icon = "plus"
shortcut = "ctrl+shift+t"     # modifiers: ctrl, shift, alt, super; keys use XKB names like Left, F5, Return
```

To use another icon, drop any [Lucide](https://lucide.dev) SVG into `static/icons/`.

**Per-app pages.** When a window of a given class is focused, its page replaces the default command keys, and workspace keys stay. The header shows which page is active. The default config ships pages for `kitty` and `firefox`:

```toml
[[apps.firefox.keys]]
label = "Back"
icon = "arrow-left"
shortcut = "alt+Left"
```

To find a window's class, run `hyprctl activewindow` and look at `class`.

**Display.** The resolution is read from the tablet over adb, and the monitor goes right of your other screens by default. Override with `display.resolution`, `display.position` (any Hyprland position, like `auto-left` or `2646x0`) and `display.scale`.

**Hardware** is auto-detected:
- network: the interface on the default route
- battery: the first `power_supply` of type Battery
- CPU temperature: `k10temp`, `zenpower`, `coretemp`, `cpu_thermal` or `acpitz`
- GPU: NVIDIA through `nvidia-smi`, AMD through sysfs

Override any of them under `[hardware]`. A card shows "Not available" or "No battery" when there's nothing to read.

## Security

The server has no authentication. It only listens on `127.0.0.1`, and the tablet reaches it through the USB tunnel. Any local user or process on the PC can call it.

The client can't run arbitrary commands:
- actions are a fixed list on the server, and the client only sends an id
- window addresses are checked against a strict pattern
- static files can't be served from outside `static/`

Don't bind it to `0.0.0.0` without adding authentication.

## Limitations

- Hyprland only. Sway, KDE and GNOME aren't supported.
- USB only. Wireless adb works in principle but proved unreliable in testing.
- Intel GPUs don't report utilization, so the Graphics card shows "Not available".
- The display runs at a fixed 60 Hz, and the virtual monitor is always named `TAB`.

VNC sends a single pointer, so touch on the display works as mouse input. Multi-touch gestures don't reach Linux apps.

## Roadmap

- Edit keys from the tablet instead of the config file
- AUR package

## Credits

Built by Akhilesh Tiwari ([@silky-x0](https://github.com/silky-x0)) together with Claude, Anthropic's AI model, in Claude Code.

## License

MIT, see [LICENSE](LICENSE). Bundled third-party assets are listed in [NOTICE](NOTICE).
