# README.md

```markdown
# screnrec.py

A simple **CLI screen recorder for Linux** — records at **30 FPS** at the
highest resolution your machine can sustain, with **monitor selection**
(up to 3 displays, single or multi-select).

No GUI, no config files — just run it, answer a few questions, and record.

---

## Features

- **Automatic backend** picked from your session:
  - **X11** → `ffmpeg` (`x11grab`)
  - **Wayland (wlroots)** — sway, Hyprland, river, wayfire, labwc, niri, … → `wf-recorder`
  - **Wayland (GNOME)** → `gnome-shell` Screencast over D-Bus
- **Monitor selection** — record one display, or multi-select several
  (up to **3**). Multiple X11 monitors are stitched into one video using
  the bounding box of the selected displays.
- **Auto resolution** — tests the native size at 30 FPS and steps down
  through a ladder (4K → 1440p → 1080p → …) until it holds the target.
- **Sound** — PulseAudio or ALSA, with fallback to no-audio if neither opens.
- **Mouse cursor** — on/off (X11 and GNOME; `wf-recorder` always draws it).
- **Save anywhere** — default `/tmp/vids`, override any time.
- **Interactive or non-interactive** — answer questions, or drive it entirely
  from flags for scripting.
- **Stop from another terminal** — `screnrec.py -stop` (PID-tracked).
- **Live prompt** while recording: `stop`, `status`, `help`.

---

## Requirements

| Session | Required packages |
|---|---|
| X11 | `ffmpeg`, `x11-xserver-utils` (or `xorg-xrandr`) |
| Wayland (wlroots) | `wf-recorder` |
| Wayland (GNOME) | `gdbus` (usually already installed with GNOME) |

### Install examples

```bash
# Debian / Ubuntu
sudo apt install ffmpeg x11-xserver-utils
sudo apt install wf-recorder            # sway / Hyprland / river / ...

# Fedora
sudo dnf install ffmpeg xorg-x11-server-utils
sudo dnf install wf-recorder

# Arch
sudo pacman -S ffmpeg xorg-xrandr wf-recorder

# openSUSE
sudo zypper install ffmpeg xrandr wf-recorder
```

The script auto-detects what's available and prints install hints if
something is missing.

---

## Usage

### Interactive (recommended)

```bash
./screnrec.py -start
```

You'll be asked:

1. **Which display(s) to record?** — e.g. `1`, `1,2`, `1 3`, or `all`
2. **Record sound?** — `[y/N]`
3. **Record mouse cursor?** — `[Y/n]`
4. **Save folder** — `Enter` for `/tmp/vids`, or type a path

Then recording starts and you get a `screnrec>` prompt.

### Non-interactive (flags)

```bash
# Record Display 1 only, with sound, no cursor, into ~/Videos
./screnrec.py -start -y --sound --no-mouse --monitors 1 --dir ~/Videos

# Record Display 1 and 3, no sound, cursor on, into /tmp/vids
./screnrec.py -start -y --no-sound --mouse --monitors "1 3"

# Record all detected monitors (up to 3)
./screnrec.py -start -y --monitors all

# Everything at defaults (Display 1, no sound, cursor on, /tmp/vids)
./screnrec.py -start -y
```

### Stop from another terminal

```bash
./screnrec.py -stop
```

Or press **Ctrl-C** inside the recording prompt.

---

## Monitor selection

Displays are numbered **Display 1, Display 2, Display 3** (maximum 3 are
shown). The format for `--monitors` / the interactive prompt is:

| Input | Meaning |
|---|---|
| `1` | only Display 1 |
| `1,2` | Display 1 and 2 |
| `1 3` | Display 1 and 3 |
| `1-3` | Display 1 through 3 |
| `all` | every detected display (capped at 3) |

**How multi-select works per backend:**

- **X11 (`ffmpeg`)** — records the **bounding box** of the selected monitors
  as one stitched video. E.g. two 1920×1080 monitors side by side → a
  3840×1080 recording.
- **Wayland (`wf-recorder`)** — `wf-recorder` records one output at a time,
  so multi-select falls back to the **first** selected monitor and prints
  a warning.
- **Wayland (GNOME)** — the GNOME Screencast D-Bus API records the **full
  screen**; monitor selection is ignored (warning printed).

---

## Command-line reference

```
screnrec.py -start [options]
screnrec.py -stop
```

| Flag | Description |
|---|---|
| `-start`, `--start` | Ask setup questions, start recording, open `screnrec>` prompt |
| `-stop`, `--stop` | Stop the running recording (from another terminal) |
| `-y`, `--yes` | Skip all questions; use flags / defaults |
| `--sound` / `--no-sound` | Record sound / don't (default: off) |
| `--mouse` / `--no-mouse` | Record cursor / don't (default: on) |
| `--dir PATH` | Save folder (default: `/tmp/vids`) |
| `--monitors SEL` | Displays to record: `1`, `1,2`, `1 3`, `all` (max 3) |

`-stop` does not accept the `-start` options (the script errors if you mix them).

---

## During recording

A prompt `screnrec>` accepts:

| Command | Action |
|---|---|
| `stop` / `exit` / `quit` / `q` | Stop and save |
| `status` | Show elapsed time, file size, and output path |
| `help` / `?` | Show these commands |
| **Ctrl-C** | Stop and save |

---

## Where files go

Default: **`/tmp/vids/`** (created automatically). Filename pattern:

```
rec_YYYYMMDD_HHMMSS.mp4     (X11 / wf-recorder)
rec_YYYYMMDD_HHMMSS.webm    (GNOME)
```

Use `--dir` or answer the "Save folder" question to pick somewhere else.

---

## How the auto-resolution works

On X11 the script first tries to record at your **native** size, scales it
down through a ladder, and runs a short 4-second test per step:

```
3840×2160 → 2560×1440 → 1920×1080 → 1600×900 → 1280×720 → 854×480
```

The first step that sustains **≥ 95 % of 30 FPS** is chosen. If none pass,
the lowest step is used with a warning. Output is always encoded with
`libx264 -preset ultrafast -crf 23` at 30 FPS.

---

## Files on disk

| File | Purpose |
|---|---|
| `/tmp/screnrec.pid` | PID + current output path of the running recorder |
| `/tmp/screnrec_ffmpeg.log` | `ffmpeg` / `wf-recorder` log (errors go here) |
| `/tmp/vids/rec_*.mp4` | Recordings |

If `ffmpeg`/`wf-recorder` fails to start, run `-start` again and look at
`/tmp/screnrec_ffmpeg.log` for details.

---

## Troubleshooting

### "Cannot detect screen size"
Install `x11-xserver-utils` (Debian/Ubuntu) or `xorg-xrandr` (Arch).

### "ffmpeg failed to start"
Check `/tmp/screnrec_ffmpeg.log`. Common causes:
- Another `x11grab` running
- Wrong `$DISPLAY`
- Audio device busy (script retries without audio automatically)

### Wayland: "no working backend"
- **sway / Hyprland / river / wayfire / labwc / niri** → install `wf-recorder`.
- **GNOME / KDE Plasma Wayland** → CLI capture is blocked. Log in with an
  **X11 / Xorg** session and rerun, or use the compositor's own recorder.

### "Already running"
Another instance is recording. Stop it with `screnrec.py -stop`, or
remove a stale `/tmp/screnrec.pid` if the process really is gone.

### Monitor list looks wrong / empty
- X11: check `xrandr --listmonitors` manually.
- Wayland: check `wlr-randr` (wlroots) — GNOME/Mutter doesn't expose this,
  so selection is limited there.

### Recording is choppy
The auto-resolution test should already step down. If not, try a smaller
display selection (e.g. just Display 1), or lower `FPS` at the top of the
script.

---

## Examples

```bash
# Quick interactive record
./screnrec.py -start

# Record Display 2 only, with sound, cursor on, into ~/Videos
./screnrec.py -start -y --sound --mouse --monitors 2 --dir ~/Videos

# Record the left + right monitors as one stitched video
./screnrec.py -start -y --monitors "1 3"

# Stop from another terminal
./screnrec.py -stop
```

---

## License

MIT — do whatever you want.
```
