#!/usr/bin/env python3
"""
screnrec.py - CLI screen recorder for Linux

Interactive:
  screnrec.py -start     ask setup questions in the terminal, then record
  screnrec.py -stop      stop the running recording (from another terminal)

Non-interactive (all flags optional; omitted ones default to sensible values):
  screnrec.py -start -y --sound --mouse --dir /tmp/vids --monitors 1,2
  screnrec.py -start -y --no-sound --no-mouse --monitors all

Default save folder: /tmp/vids (created automatically), 30 FPS,
highest resolution the machine can sustain.

Monitor selection:
  Up to 3 displays are shown as Display 1, Display 2, Display 3.
  You can pick one, or multi-select (comma/space separated) to record
  several monitors side by side into a single video.

Backends (picked automatically):
  X11 session          -> ffmpeg (x11grab)
  Wayland (wlroots)    -> wf-recorder   (sway, Hyprland, river, wayfire ...)
  Wayland (GNOME)      -> gnome-shell Screencast D-Bus
"""

import argparse
import datetime
import os
import re
import shutil
import signal
import subprocess
import sys
import time

OUT_DIR = "/tmp/vids"
PID_FILE = "/tmp/screnrec.pid"
FFMPEG_LOG = "/tmp/screnrec_ffmpeg.log"
FPS = 30
MAX_MONITORS = 3

# Resolutions to fall back to (16:9) if the native one is too heavy for 30 FPS
LADDER = [(3840, 2160), (2560, 1440), (1920, 1080), (1600, 900),
          (1280, 720), (854, 480)]


class StopRequest(Exception):
    pass


def _raise_stop(signum, frame):
    raise StopRequest()


def have(cmd):
    return shutil.which(cmd) is not None


def even(n):
    return int(n) // 2 * 2


# --------------------------------------------------------------------------
# Monitor detection
# --------------------------------------------------------------------------
def list_monitors():
    """Return a list of dicts: {index, name, x, y, w, h, primary}.

    Uses xrandr (X11) or wlr-randr / gnome-monitor-config (Wayland).
    Falls back to a single "screen" entry if nothing can be enumerated.
    """
    monitors = []

    # X11: xrandr --listmonitors  (preferred - gives logical monitors)
    if have("xrandr"):
        try:
            out = subprocess.run(["xrandr", "--listmonitors"],
                                 capture_output=True, text=True).stdout
            # Lines like:
            #   0: +*eDP-1 1920/344x1080/194+0+0  eDP-1
            #   1: +HDMI-1 2560/597x1440/336+1920+0  HDMI-1
            for line in out.splitlines()[1:]:
                m = re.match(
                    r"\s*(\d+):\s+\+?\*?(\S+)\s+"
                    r"(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)\s+(\S+)",
                    line)
                if not m:
                    continue
                idx, name, w, h, x, y, outname = m.groups()
                monitors.append({
                    "index": len(monitors) + 1,
                    "name": outname or name,
                    "w": int(w), "h": int(h),
                    "x": int(x), "y": int(y),
                    "primary": "*" in line.split()[1],
                })
        except OSError:
            pass

    # X11 fallback: xrandr --query
    if not monitors and have("xrandr"):
        try:
            out = subprocess.run(["xrandr", "--query"],
                                 capture_output=True, text=True).stdout
            for line in out.splitlines():
                m = re.match(r"^(\S+) connected (primary )?(\d+)x(\d+)\+(\d+)\+(\d+)",
                             line)
                if not m:
                    continue
                name, prim, w, h, x, y = m.groups()
                monitors.append({
                    "index": len(monitors) + 1,
                    "name": name,
                    "w": int(w), "h": int(h),
                    "x": int(x), "y": int(y),
                    "primary": bool(prim),
                })
        except OSError:
            pass

    # Wayland: wlr-randr
    if not monitors and have("wlr-randr"):
        try:
            out = subprocess.run(["wlr-randr"],
                                 capture_output=True, text=True).stdout
            cur = None
            for line in out.splitlines():
                if not line.startswith(" "):
                    if cur:
                        monitors.append(cur)
                    cur = {"index": len(monitors) + 1,
                           "name": line.strip(),
                           "w": 0, "h": 0, "x": 0, "y": 0,
                           "primary": False}
                elif cur is not None:
                    m = re.search(r"(\d+)x(\d+) px", line)
                    if m:
                        cur["w"], cur["h"] = int(m.group(1)), int(m.group(2))
                    if "Enabled: yes" in line:
                        cur["enabled"] = True
            if cur:
                monitors.append(cur)
        except OSError:
            pass

    # Give monitors that had no offset a sequential x position
    x_cursor = 0
    for mon in monitors:
        if mon["w"] and mon["h"]:
            if mon["x"] == 0 and mon["y"] == 0 and x_cursor:
                mon["x"] = x_cursor
            x_cursor = max(x_cursor, mon["x"] + mon["w"])

    # Fallback: single screen entry
    if not monitors:
        size = x11_screen_size()
        if size:
            monitors.append({
                "index": 1, "name": "screen",
                "w": size[0], "h": size[1],
                "x": 0, "y": 0, "primary": True,
            })

    # Trim to MAX_MONITORS
    return monitors[:MAX_MONITORS]


def parse_monitor_choice(text, count):
    """Parse 'all' / '1,2' / '1 2' / '1-3' into a sorted list of 1-based indexes.

    Returns (selection, error).
    """
    text = text.strip().lower()
    if not text:
        return None, "No selection given"
    if text == "all":
        return list(range(1, count + 1)), None

    chosen = set()
    for part in re.split(r"[,\s]+", text):
        if not part:
            continue
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                a, b = int(a), int(b)
            except ValueError:
                return None, f"Bad range: {part}"
            if a > b:
                a, b = b, a
            for i in range(a, b + 1):
                chosen.add(i)
        else:
            try:
                chosen.add(int(part))
            except ValueError:
                return None, f"Bad number: {part}"

    if not chosen:
        return None, "No monitors selected"
    bad = [i for i in chosen if i < 1 or i > count]
    if bad:
        return None, f"Monitor(s) out of range: {sorted(bad)} (max {count})"
    if len(chosen) > MAX_MONITORS:
        return None, f"Too many monitors selected (max {MAX_MONITORS})"
    return sorted(chosen), None


def ask_monitors():
    """Show numbered monitors and ask which to record."""
    monitors = list_monitors()
    if not monitors:
        print("[!] No monitors detected - recording the whole screen.")
        return []

    print("Detected displays:")
    for mon in monitors:
        tag = " [primary]" if mon["primary"] else ""
        geom = f"{mon['w']}x{mon['h']} at ({mon['x']},{mon['y']})" \
            if mon["w"] and mon["h"] else "(size unknown)"
        print(f"  Display {mon['index']}: {mon['name']}  {geom}{tag}")

    n = len(monitors)
    print(f"\nWhich display(s) to record?  (1-{n}, "
          f"comma/space separated, or 'all'; max {MAX_MONITORS})")
    print("  Examples: 1   |   1,2   |   1 3   |   all")

    while True:
        ans = input("Monitors to record [1]: ").strip()
        if not ans:
            ans = "1"
        sel, err = parse_monitor_choice(ans, n)
        if err:
            print("  [!] " + err)
            continue
        return sel


# --------------------------------------------------------------------------
# Setup questions (plain CLI, no curses)
# --------------------------------------------------------------------------
def check_dir(path):
    """Create the folder if needed. Returns (abs_path, error)."""
    p = os.path.abspath(os.path.expanduser(path.strip()))
    try:
        os.makedirs(p, exist_ok=True)
    except OSError as e:
        return None, f"Cannot create folder: {e.strerror}"
    if not os.access(p, os.W_OK):
        return None, "Folder is not writable"
    return p, None


def ask_yn(question, default=None):
    """Ask y/n. Returns bool. EOF/KeyboardInterrupt -> raise."""
    suffix = " [Y/n]: " if default is True else \
             " [y/N]: " if default is False else " (y/n): "
    while True:
        ans = input(question + suffix).strip().lower()
        if ans == "" and default is not None:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("  please answer y or n")


def ask_path(default):
    """Ask for a folder. Empty input -> default."""
    while True:
        p = input(f"Save folder [{default}]: ").strip()
        if not p:
            p = default
        path, err = check_dir(p)
        if err:
            print("  [!] " + err)
            continue
        return path


def resolve_monitors_flag(flag_value):
    """Turn the --monitors flag into a selection list. None -> ask interactively."""
    monitors = list_monitors()
    if flag_value is None:
        return ask_monitors()
    if not monitors:
        # No monitors to map to - let build_recorder handle fallback
        return []
    sel, err = parse_monitor_choice(flag_value, len(monitors))
    if err:
        print("[!] " + err)
        return ask_monitors()
    return sel


def cli_setup(args):
    """Interactive CLI setup. Uses flag values where provided."""
    sound = args.sound
    mouse = args.mouse
    folder = args.dir

    if args.yes:
        # Nothing to ask except monitors if not supplied
        sel = resolve_monitors_flag(args.monitors) if args.monitors is not None \
            else None
        if sel is None:
            # -y but --monitors not given -> default to Display 1
            sel = [1]
        return {"sound": bool(sound), "mouse": bool(mouse),
                "dir": folder or OUT_DIR, "monitors": sel}

    print("=" * 50)
    print("  SCREENREC  -  Setup")
    print("=" * 50)

    # Monitors first
    if args.monitors is not None:
        sel = resolve_monitors_flag(args.monitors)
        print(f"Monitors to record:    {', '.join('Display ' + str(i) for i in sel)}")
    else:
        sel = ask_monitors()
    monitors = list_monitors()
    names = [monitors[i - 1]["name"] for i in sel if i - 1 < len(monitors)]
    print(f"  -> {', '.join(names) if names else 'full screen'}")

    if sound is None:
        sound = ask_yn("Record sound?", default=False)
    else:
        print(f"Record sound?          {'YES' if sound else 'NO'}")

    if mouse is None:
        mouse = ask_yn("Record mouse cursor?", default=True)
    else:
        print(f"Record mouse cursor?   {'YES' if mouse else 'NO'}")

    if folder:
        path, err = check_dir(folder)
        if err:
            print("[!] " + err)
            path = ask_path(OUT_DIR)
        else:
            print(f"Save folder            {path}")
    else:
        path = ask_path(OUT_DIR)

    print("-" * 50)
    print(f"  Monitors: {', '.join('Display ' + str(i) for i in sel)}")
    print(f"  Sound   : {'on' if sound else 'off'}")
    print(f"  Mouse   : {'on' if mouse else 'off'}")
    print(f"  Folder  : {path}")
    print("=" * 50)
    return {"sound": sound, "mouse": mouse, "dir": path, "monitors": sel}


# --------------------------------------------------------------------------
# Environment detection
# --------------------------------------------------------------------------
def session_type():
    st = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if st in ("x11", "wayland"):
        return st
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


def desktop_name():
    return (os.environ.get("XDG_CURRENT_DESKTOP", "") + " " +
            os.environ.get("DESKTOP_SESSION", "")).lower()


def x11_screen_size():
    """Full X screen size (all monitors) -> (w, h) or None."""
    if have("xrandr"):
        out = subprocess.run(["xrandr", "--current"], capture_output=True,
                             text=True).stdout
        m = re.search(r"current (\d+) x (\d+)", out)
        if m:
            return int(m.group(1)), int(m.group(2))
    if have("xdpyinfo"):
        out = subprocess.run(["xdpyinfo"], capture_output=True,
                             text=True).stdout
        m = re.search(r"dimensions:\s+(\d+)x(\d+)", out)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


# --------------------------------------------------------------------------
# Compute the X11 grab region covering one or more monitors
# --------------------------------------------------------------------------
def compute_grab_region(monitors, sel):
    """Return (x, y, w, h, description) covering the selected monitors.

    If ``sel`` is empty, fall back to the whole X screen.
    """
    if not sel or not monitors:
        size = x11_screen_size()
        if size:
            return 0, 0, size[0], size[1], "full screen"
        return None

    picked = [monitors[i - 1] for i in sel if 1 <= i <= len(monitors)]
    if not picked:
        return compute_grab_region(monitors, [])

    # Bounding box of selected monitors
    x0 = min(m["x"] for m in picked)
    y0 = min(m["y"] for m in picked)
    x1 = max(m["x"] + m["w"] for m in picked)
    y1 = max(m["y"] + m["h"] for m in picked)
    w = even(x1 - x0)
    h = even(y1 - y0)
    if w <= 0 or h <= 0:
        # Any selected monitor has unknown size -> full screen
        size = x11_screen_size()
        if size:
            return 0, 0, size[0], size[1], "full screen (size unknown)"
        return None

    names = ", ".join(m["name"] for m in picked)
    return x0, y0, w, h, names


# --------------------------------------------------------------------------
# Find the highest resolution that holds 30 FPS (X11 / ffmpeg)
# --------------------------------------------------------------------------
def ffmpeg_x11_cmd(display, gx, gy, gw, gh, w, h, out, duration=None,
                   mouse=True, audio=None):
    """audio = None or (format, device), e.g. ("pulse", "default").

    gx,gy,gw,gh = grab region (offset + size).
    w,h         = output (scaled) size.
    """
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "info", "-stats",
           "-thread_queue_size", "512",
           "-f", "x11grab", "-framerate", str(FPS),
           "-draw_mouse", "1" if mouse else "0",
           "-video_size", f"{gw}x{gh}",
           "-i", f"{display}+{gx},{gy}"]
    if audio:
        cmd += ["-thread_queue_size", "512", "-f", audio[0], "-i", audio[1]]
    if duration:
        cmd += ["-t", str(duration)]
    cmd += ["-vf", f"scale={w}:{h}",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-r", str(FPS)]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    if out is None:
        cmd += ["-f", "null", "-"]
    else:
        cmd += [out]
    return cmd


def can_do_30fps(display, gx, gy, gw, gh, w, h):
    cmd = ffmpeg_x11_cmd(display, gx, gy, gw, gh, w, h, None, duration=4)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return False
    fps_vals = re.findall(r"fps=\s*([\d.]+)", r.stderr)
    if r.returncode != 0 or not fps_vals:
        return False
    return float(fps_vals[-1]) >= FPS * 0.95


def pick_best_resolution(display, gx, gy, gw, gh):
    candidates = [(even(gw), even(gh))]
    ratio = gh / gw if gw else 9 / 16
    for lw, _ in LADDER:
        if lw < gw:
            candidates.append((lw, even(lw * ratio)))
    print(f"[*] Grab region: {gw}x{gh}. "
          f"Testing what it can record at {FPS} FPS...")
    for (w, h) in candidates:
        print(f"    testing {w}x{h} ... ", end="", flush=True)
        if can_do_30fps(display, gx, gy, gw, gh, w, h):
            print("OK")
            return w, h
        print("too slow")
    w, h = candidates[-1]
    print(f"[!] Nothing passed the test, using lowest: {w}x{h}")
    return w, h


# --------------------------------------------------------------------------
# Recorder backends
# --------------------------------------------------------------------------
class ProcRecorder:
    """ffmpeg / wf-recorder child process."""

    def __init__(self, cmd, out, label):
        self.cmd, self.out, self.label = cmd, out, label
        self.proc = None

    def start(self):
        log = open(FFMPEG_LOG, "wb")
        try:
            self.proc = subprocess.Popen(self.cmd, stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=log,
                                         start_new_session=True)
        finally:
            log.close()
        time.sleep(1.5)
        return self.proc.poll() is None

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if not self.proc:
            return
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)   # lets it finalize the file
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    try:
                        self.proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass


class GnomeRecorder:
    """GNOME Shell built-in screencast over D-Bus (Wayland)."""

    def __init__(self, out, mouse):
        self.out = out
        self.mouse = mouse
        self.label = "GNOME Shell Screencast"
        self.started = False

    def _call(self, method, *args):
        return subprocess.run(
            ["gdbus", "call", "--session",
             "--dest", "org.gnome.Shell.Screencast",
             "--object-path", "/org/gnome/Shell/Screencast",
             "--method", f"org.gnome.Shell.Screencast.{method}", *args],
            capture_output=True, text=True)

    def start(self):
        if not have("gdbus"):
            return False
        cursor = "true" if self.mouse else "false"
        r = self._call("Screencast", self.out,
                       f"{{'framerate': <{FPS}>, 'draw-cursor': <{cursor}>}}")
        self.started = r.returncode == 0 and "true" in r.stdout
        return self.started

    def alive(self):
        return self.started

    def stop(self):
        if self.started:
            self._call("StopScreencast")
            self.started = False


def log_tail():
    try:
        with open(FFMPEG_LOG, errors="replace") as f:
            return f.read()[-1200:]
    except OSError:
        return ""


def build_recorder(out_base, opts):
    """Starts the recorder. Returns ((recorder, info), None) or (None, error)."""
    st = session_type()
    desk = desktop_name()
    sound, mouse = opts["sound"], opts["mouse"]
    sel = opts.get("monitors", [])

    monitors = list_monitors()

    # ---------------- X11 ----------------
    if st == "x11":
        if not have("ffmpeg"):
            return None, install_hint("ffmpeg")
        display = os.environ.get("DISPLAY", ":0")
        region = compute_grab_region(monitors, sel)
        if not region:
            return None, ("Cannot detect screen size "
                          "(install x11-xserver-utils / xorg-xrandr).")
        gx, gy, gw, gh, desc = region
        w, h = pick_best_resolution(display, gx, gy, gw, gh)
        out = out_base + ".mp4"

        audio_tries = [("pulse", "default"), ("alsa", "default"), None] \
            if sound else [None]
        for a in audio_tries:
            cmd = ffmpeg_x11_cmd(display, gx, gy, gw, gh, w, h, out,
                                 mouse=mouse, audio=a)
            rec = ProcRecorder(cmd, out, "ffmpeg x11grab")
            if rec.start():
                if sound and a is None:
                    print("[!] Could not open an audio device - "
                          "recording WITHOUT sound.")
                snd = f"sound:{a[0]}" if a else "sound:off"
                return (rec, f"{desc} ({gw}x{gh} -> {w}x{h} @ {FPS} FPS, "
                             f"{snd}, mouse:{'on' if mouse else 'off'})"), None
        return None, "ffmpeg failed to start. Log:\n" + log_tail()

    # ---------------- Wayland ----------------
    if st == "wayland":
        wlroots = any(k in desk for k in
                      ("sway", "hypr", "river", "wayfire", "labwc", "niri",
                       "wlroots", "dwl", "cosmic"))
        options = ["gnome", "wf"] if "gnome" in desk else ["wf", "gnome"]
        if wlroots:
            options = ["wf", "gnome"]

        # Pick names for the selected monitors (used by wf-recorder -o)
        picked_names = []
        for i in sel:
            if 1 <= i <= len(monitors):
                picked_names.append(monitors[i - 1]["name"])

        for opt in options:
            if opt == "wf" and have("wf-recorder"):
                out = out_base + ".mp4"
                cmd = ["wf-recorder", "-r", str(FPS), "-c", "libx264",
                       "-f", out]
                # Multi-monitor with wf-recorder: one output per selected
                # monitor -> wf-recorder supports -o NAME (one at a time).
                # For simplicity, if more than one monitor is chosen, we
                # record the first one and warn - wf-recorder can't stitch
                # multiple outputs on its own.
                if len(picked_names) == 1:
                    cmd += ["-o", picked_names[0]]
                elif len(picked_names) > 1:
                    print("[!] wf-recorder cannot record several monitors "
                          "at once - using the first one: "
                          f"{picked_names[0]}")
                    cmd += ["-o", picked_names[0]]
                if sound:
                    cmd.append("-a")
                rec = ProcRecorder(cmd, out, "wf-recorder")
                if rec.start():
                    if not mouse:
                        print("[!] wf-recorder cannot hide the mouse "
                              "- it will be recorded.")
                    where = picked_names[0] if picked_names else "all outputs"
                    return (rec, f"{where} @ {FPS} FPS, "
                                 f"sound:{'on' if sound else 'off'}"), None
            if opt == "gnome":
                out = out_base + ".webm"
                rec = GnomeRecorder(out, mouse)
                if rec.start():
                    if sound:
                        print("[!] GNOME Screencast has no audio support "
                              "- recording WITHOUT sound.")
                    if len(picked_names) > 1:
                        print("[!] GNOME Screencast records the full screen "
                              "- monitor selection is ignored.")
                    return (rec, f"full screen @ {FPS} FPS, sound:off, "
                                 f"mouse:{'on' if mouse else 'off'}"), None

        return None, (
            "Wayland session detected but no working backend.\n"
            "  - wlroots compositors (sway/Hyprland...): install wf-recorder\n"
            "  - GNOME/KDE: Wayland blocks screen capture for CLI tools;\n"
            "    log in with an 'X11 / Xorg' session and run again."
        )

    return None, ("No graphical session found "
                  "(DISPLAY / WAYLAND_DISPLAY not set).")


def install_hint(pkg):
    return (f"{pkg} is not installed. Install it:\n"
            f"  Debian/Ubuntu : sudo apt install {pkg}\n"
            f"  Fedora        : sudo dnf install {pkg}\n"
            f"  Arch          : sudo pacman -S {pkg}\n"
            f"  openSUSE      : sudo zypper install {pkg}")


# --------------------------------------------------------------------------
# PID file helpers
# --------------------------------------------------------------------------
def read_state():
    try:
        with open(PID_FILE) as f:
            lines = f.read().splitlines()
        pid = int(lines[0])
        path = lines[1] if len(lines) > 1 else ""
    except (OSError, ValueError, IndexError):
        return None, ""
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return None, ""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            if b"screnrec" not in f.read():
                return None, ""
    except OSError:
        return None, ""
    return pid, path


def write_state(path):
    with open(PID_FILE, "w") as f:
        f.write(f"{os.getpid()}\n{path}\n")


def clear_state():
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def cmd_start(args):
    pid, _ = read_state()
    if pid:
        print(f"[!] Already running (PID {pid}). Use: {sys.argv[0]} -stop")
        return 1

    # 1) Setup (interactive or flag-driven)
    try:
        opts = cli_setup(args)
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        return 1
    if not opts:
        print("Cancelled.")
        return 1

    # 2) Folder
    out_dir, e = check_dir(opts["dir"])
    if e:
        print("[!] " + e)
        return 1
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_base = os.path.join(out_dir, f"rec_{stamp}")

    # 3) Start recorder
    result, err = build_recorder(out_base, opts)
    if err:
        print("[!] " + err)
        return 1
    rec, info = result

    write_state(rec.out)
    signal.signal(signal.SIGTERM, _raise_stop)
    signal.signal(signal.SIGINT, _raise_stop)

    t0 = time.time()
    print(f"[+] Recording started ({rec.label}: {info})")
    print(f"[+] File: {rec.out}")
    print("Commands: stop | status | help   (Ctrl-C also stops)")

    try:
        while True:
            if not rec.alive():
                print("\n[!] Recorder stopped unexpectedly.")
                break
            try:
                line = input("screnrec> ").strip().lower()
            except EOFError:
                break
            if line in ("stop", "exit", "quit", "q"):
                break
            elif line == "status":
                el = int(time.time() - t0)
                try:
                    size = os.path.getsize(rec.out) if os.path.exists(rec.out) else 0
                except OSError:
                    size = 0
                print(f"Recording  {el // 60:02d}:{el % 60:02d}  "
                      f"{human(size)}  -> {rec.out}")
            elif line in ("help", "?"):
                print("stop   - stop recording and save\n"
                      "status - show elapsed time and file size\n"
                      "help   - this message")
            elif line:
                print("Unknown command. Type 'help'.")
    except (StopRequest, KeyboardInterrupt):
        print()
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        print("[*] Stopping...")
        rec.stop()
        clear_state()

    if os.path.exists(rec.out):
        try:
            size = os.path.getsize(rec.out)
        except OSError:
            size = 0
        print(f"[+] Saved: {rec.out} ({human(size)})")
    else:
        print("[!] Output file not found.")
    return 0


def cmd_stop():
    pid, path = read_state()
    if not pid:
        print("[!] No recording is running.")
        clear_state()
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print("[!] No recording is running.")
        clear_state()
        return 1
    exited = False
    for _ in range(60):
        time.sleep(0.25)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            exited = True
            break
        try:
            with open(f"/proc/{pid}/stat") as f:
                state = f.read().split()[2]
            if state == "Z":
                exited = True
                break
        except OSError:
            exited = True
            break
    if not exited:
        print("[!] Process did not exit in time.")
        return 1
    print("[+] Recording stopped.")
    if path and os.path.exists(path):
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        print(f"[+] Saved: {path} ({human(size)})")
    return 0


def main():
    ap = argparse.ArgumentParser(
        prog="screnrec.py",
        description="CLI screen recorder for Linux (30 FPS, best resolution).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("-start", "--start", action="store_true",
                   help="ask setup questions, start recording, open screnrec> prompt")
    g.add_argument("-stop", "--stop", action="store_true",
                   help="stop the running recording")

    # Non-interactive options for -start
    ap.add_argument("-y", "--yes", action="store_true",
                    help="skip all questions; use flags/defaults")
    ap.add_argument("--sound", dest="sound", action="store_true",
                    default=None, help="record sound (non-interactive)")
    ap.add_argument("--no-sound", dest="sound", action="store_false",
                    help="don't record sound (non-interactive)")
    ap.add_argument("--mouse", dest="mouse", action="store_true",
                    default=None, help="record mouse cursor (non-interactive)")
    ap.add_argument("--no-mouse", dest="mouse", action="store_false",
                    help="don't record mouse cursor (non-interactive)")
    ap.add_argument("--dir", "--folder", dest="dir", default=None,
                    metavar="PATH", help=f"save folder (default: {OUT_DIR})")
    ap.add_argument("--monitors", "--monitor", dest="monitors", default=None,
                    metavar="SEL",
                    help="display(s) to record: e.g. 1  |  1,2  |  1 3  |  "
                         f"all  (max {MAX_MONITORS})")

    args = ap.parse_args()

    if args.stop:
        if (args.yes or args.sound is not None or args.mouse is not None
                or args.dir is not None or args.monitors is not None):
            ap.error("-stop does not accept -y / --sound / --mouse / "
                     "--dir / --monitors")
        sys.exit(cmd_stop())

    sys.exit(cmd_start(args))


if __name__ == "__main__":
    main()
