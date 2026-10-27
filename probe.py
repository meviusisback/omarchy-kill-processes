#!/usr/bin/env python3
"""Process Pulse - snapshot of the running processes, grouped by app.

Reads /proc once per tick and diffs against the previous tick and emits one JSON document with:

  * processes grouped by application (systemd cgroup when available,
    process tree otherwise, wrapper chains collapsed into one row),
  * per-group CPU / RAM / thread totals,
  * problem detection: zombies, blocked (D state) and runaway (a process
    pinned to a core for many consecutive samples = probable busy loop),
  * the full PID list of every group, so the panel can kill a whole
    application tree with one action.

Only the standard library is used and nothing is executed from a shell.
The state file lives under ~/.local/state/omarchy-killprocs/ and holds
the previous tick plus per-PID identity (start time + uid) used by
kill.py to refuse signalling a recycled PID.

Usage:
  probe.py snapshot [--interval S] [--runaway-samples N]
"""

import argparse
import json
import math
import os
import re
import sys
import time

STATE_VERSION = 1
STATE_MAX_AGE_S = 120.0
# Clock ticks per second: the CPU%% and age maths must divide by the
# real clock, never by a hardcoded 100.
try:
    USER_HZ = float(os.sysconf("SC_CLK_TCK"))
except (ValueError, OSError, AttributeError):
    USER_HZ = 100.0
PID_CAP = 2048
MAX_GROUPS = 60
MAX_PROCS_PER_GROUP = 12
CMD_ARGS_MAX = 10
NAME_LEN = 48
PREVIEW_LEN = 140
RUNAWAY_CPU = 95.0
BLOCKED_SAMPLES = 3
SWARM_PROCS = 40
PAGE_KIB = os.sysconf("SC_PAGE_SIZE") // 1024 if hasattr(os, "sysconf") else 4

FRIENDLY = {
    "chrome": "Google Chrome", "chromium": "Chromium", "brave": "Brave",
    "firefox": "Firefox", "thunderbird": "Thunderbird", "opera": "Opera",
    "vivaldi": "Vivaldi", "microsoft-edge": "Microsoft Edge", "msedge": "Microsoft Edge",
    "electron": "Electron app", "code": "Visual Studio Code", "code-url-handler": "Visual Studio Code",
    "codium": "VSCodium", "zeditor": "Zed", "sublime_text": "Sublime Text",
    "nvim": "Neovim", "vim": "Vim", "kitty": "Kitty", "alacritty": "Alacritty",
    "wezterm": "WezTerm", "foot": "Foot", "ghostty": "Ghostty", "rio": "Rio",
    "konsole": "Konsole", "xterm": "xterm", "tmux": "tmux", "tmux: server": "tmux",
    "slack": "Slack", "discord": "Discord", "signal": "Signal", "telegram-desktop": "Telegram",
    "spotify": "Spotify", "obsidian": "Obsidian", "notion-app": "Notion",
    "steam": "Steam", "steamwebhelper": "Steam", "lutris": "Lutris",
    "mpv": "mpv", "vlc": "VLC", "gimp": "GIMP", "inkscape": "Inkscape",
    "blender": "Blender", "kdenlive": "Kdenlive", "obs": "OBS Studio",
    "nautilus": "Files", "dolphin": "Dolphin", "thunar": "Thunar",
    "org.gnome.nautilus": "Files", "nemo": "Files",
    "hyprland": "Hyprland", "Hyprland": "Hyprland", "quickshell": "Omarchy Shell",
    "omarchy-shell": "Omarchy Shell", "waybar": "Waybar", "swaync": "Notifications",
    "org_kde_powerdevil": "PowerDevil", "xdg-desktop-portal": "Desktop Portal",
    "systemd": "systemd", "systemd --user": "systemd (user)",
    "docker": "Docker", "containerd": "Containerd", "podman": "Podman",
    "node": "Node.js", "npm": "npm", "pnpm": "pnpm", "bun": "Bun", "deno": "Deno",
    "python": "Python", "python3": "Python", "ruby": "Ruby", "java": "Java",
    "rustc": "rustc", "cargo": "Cargo", "go": "Go", "gcc": "gcc", "g++": "g++",
    "clang": "clang", "make": "make", "cmake": "CMake", "git": "Git",
    "rg": "ripgrep", "fd": "fd", "bash": "Shell", "zsh": "Shell", "fish": "Fish",
    "sh": "Shell", "dash": "Shell", "sleep": "sleep", "dbus-daemon": "D-Bus",
    "pipewire": "PipeWire", "wireplumber": "WirePlumber", "pulseaudio": "PipeWire",
    "Xwayland": "XWayland", "xeyes": "X11 app",
}

GENERIC = {
    "sh", "bash", "zsh", "dash", "fish", "tmux", "sudo", "su", "env", "nice",
    "nohup", "setsid", "firejail", "bwrap", "flatpak", "snap", "electron",
    "python", "python3", "node", "ruby", "java", "busybox", "xdg-open",
    "systemd-run", "timeout", "perl", "lua", "wrapper", "launch", "launcher",
    "startapp", "run-app",
}

# Launchers carry no information about the app they started. Some of them
# get truncated by the 15-char comm field (gtk-launch -> launch), so the
# short form is listed too.
LAUNCHERS = {
    "gtk-launch", "launch", "gtk4-launch", "xdg-terminal-exec", "xdg-exec",
    "xdg-open", "xdg-mime", "systemd-run", "systemd --user", "systemctl",
    "dbus-run-session", "dbus-launch", "dbus-daemon", "ssh-agent",
    "sh", "bash", "zsh", "dash", "fish", "su", "sudo", "env", "setsid",
    "nohup", "nice", "timeout", "firejail", "bwrap", "flatpak", "snap",
    "electron", "wrapper", "launcher", "startapp", "weston-launch",
    "hyprland-launch", "omarchy-launch", "footclient", "gio",
}

STATE_NAMES = {"R": "running", "S": "sleeping", "D": "disk-wait", "I": "idle",
               "T": "stopped", "t": "tracing", "Z": "zombie", "X": "dead", "P": "parked"}


def clamp_text(raw, limit):
    """Printable, single-line, bounded text. Untrusted input from /proc."""
    if raw is None:
        return ""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    elif not isinstance(raw, str):
        raw = str(raw)
    out = "".join(ch for ch in raw if ch.isprintable())
    out = " ".join(out.split())
    if len(out) > limit:
        out = out[: max(0, limit - 1)] + "\u2026"
    return out


def read_text(path, limit=16384):
    try:
        with open(path, "rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def parse_stat(text):
    """(comm, fields) from /proc/<pid>/stat; fields[0] is 'state'.

    Only fields 1 and 2 (pid, comm) sit before the parenthesis, so the
    tail holds the remaining 50 entries and every index below is offset
    by 2 relative to proc(5).
    """
    left = text.find("(")
    right = text.rfind(")")
    if left < 0 or right < 0 or right < left:
        return None
    comm = text[left + 1:right]
    fields = text[right + 1:].split()
    if len(fields) < 20:
        return None
    return comm, fields


def state_dir():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    path = os.path.join(base, "omarchy-killprocs")
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError:
        return None
    return path


def state_file():
    directory = state_dir()
    return os.path.join(directory, "state.json") if directory else None


def load_state(path):
    now = time.time()
    try:
        with open(path, "rb") as handle:
            raw = handle.read(4 * 1024 * 1024)
        data = json.loads(raw.decode("utf-8", "replace"))
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        return {}
    try:
        ts = float(data.get("ts", 0))
    except (TypeError, ValueError):
        return {}
    # Bounded both ways: NaN/Inf fail isfinite, a future-dated ts fails
    # the lower bound instead of widening the acceptance window.
    if not math.isfinite(ts) or not (0.0 <= now - ts <= STATE_MAX_AGE_S):
        return {}
    return data


def save_state(path, data):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        pass


def read_stat_pid(pid):
    text = read_text("/proc/%d/stat" % pid, 4096)
    parsed = parse_stat(text)
    if not parsed:
        return None
    comm, f = parsed
    try:
        return {
            "pid": pid,
            "ppid": int(f[1]),
            "pgrp": int(f[2]),
            "session": int(f[3]),
            "utime": int(f[11]),
            "stime": int(f[12]),
            "startticks": int(f[19]),
            "nice": int(f[16]),
            "threads": int(f[17]),
            "state": f[0],
            "comm": comm,
        }
    except (ValueError, IndexError):
        return None


def read_status(pid):
    """(name, uid, rss_kib) from /proc/<pid>/status; all optional."""
    text = read_text("/proc/%d/status" % pid, 8192)
    name = uid = rss = None
    for line in text.split("\n"):
        if line.startswith("Name:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("Uid:"):
            parts = line.split(":", 1)[1].split()
            if parts:
                try:
                    uid = int(parts[0])
                except ValueError:
                    uid = None
        elif line.startswith("VmRSS:"):
            parts = line.split(":", 1)[1].split()
            if parts:
                try:
                    rss = int(parts[0])
                except ValueError:
                    rss = None
        if name is not None and uid is not None and rss is not None:
            break
    return name, uid, rss


def read_cmdline(pid):
    """argv for a userspace process; empty for kernel threads."""
    raw = read_text("/proc/%d/cmdline" % pid, 8192)
    if not raw:
        return []
    return [part for part in raw.split("\0") if part][:CMD_ARGS_MAX]


def read_cgroup(pid):
    text = read_text("/proc/%d/cgroup" % pid, 4096)
    return text.split("\n", 1)[0] if text else ""


def read_uptime():
    text = read_text("/proc/uptime", 128)
    try:
        return float(text.split()[0])
    except (ValueError, IndexError, OSError):
        return 0.0


def read_totals():
    total = busy = None
    text = read_text("/proc/stat", 4096)
    for line in text.split("\n"):
        if line.startswith("cpu "):
            parts = line.split()[1:]
            try:
                values = [int(v) for v in parts]
            except ValueError:
                break
            total = sum(values)
            busy = total - (values[3] + values[4] if len(values) > 4 else 0)
            break
    mem_total = mem_avail = 0
    for line in read_text("/proc/meminfo", 4096).split("\n"):
        if line.startswith("MemTotal:"):
            mem_total = int(line.split()[1]) * 1024
        elif line.startswith("MemAvailable:"):
            mem_avail = int(line.split()[1]) * 1024
        if mem_total and mem_avail:
            break
    loadavg = 0.0
    try:
        loadavg = float(read_text("/proc/loadavg", 64).split()[0])
    except (ValueError, IndexError, OSError):
        pass
    return total, busy, mem_total, mem_avail, loadavg


SCOPE_RE = re.compile(r"(?:^|/)(app(?:-lib)?-[^/]+?)(?:-[0-9a-f]{6,16})?\.scope$")
# app-dbus-<unique bus name>.scope carries the service in its own delimiter form.
DBUS_RE = re.compile(r"\\x2d([0-9]+)\\x2d([^\\/]+?)(?:-[0-9a-f]{6,16})?$")
GENERIC_SUFFIXES = (".desktop", ".service", ".application", ".app", ".scope")
HASH_RE = re.compile(r"^[0-9a-f]{6,16}$")


def clean_unit_name(raw):
    """hermes-gateway.service -> hermes gateway"""
    name = (raw or "").strip()
    for suffix in GENERIC_SUFFIXES:
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    if "@" in name:
        # systemd template instance: drop the instance id (a hash for the
        # plugin's own workers), keep the unit name.
        name = name.split("@", 1)[0]
    # A long hex tail is an instance hash (hermes-worker-proc-891226b80642),
    # not part of the name.
    name = re.sub(r"[-_ ]+[0-9a-f]{8,}$", "", name)
    return re.sub(r"[-_]+", " ", name).strip()


def clean_app_name(raw):
    """org.gnome.Nautilus.desktop -> Nautilus"""
    name = (raw or "").strip()
    lowered = name.lower()
    for suffix in GENERIC_SUFFIXES:
        if lowered.endswith(suffix):
            name = name[: -len(suffix)]
            break
    if "@" in name:
        name = name.rsplit("@", 1)[1]
    name = name.strip().strip(".-_ ")
    if not name:
        return ""
    parts = [p for p in re.split(r"[.\-_/]", name) if p]
    if not parts:
        return ""
    candidate = parts[-1]
    if len(candidate) < 2 and len(parts) > 1:
        candidate = parts[-2]
    # Some scopes append an instance hash (hermes-worker-proc-891226b80):
    # a long hex tail carries no name, so drop it.
    return re.sub(r"[-_ ]+[0-9a-f]{8,}$", "", candidate) or candidate


def app_key(cgroup_line):
    """Stable application identity from the cgroup, if any."""
    match = SCOPE_RE.search(cgroup_line)
    if match:
        scope = match.group(1)
        # Hyprland wraps the app id with its launcher and a random hash.
        if "\\x2d" in scope:
            tail = scope.rsplit("\\x2d", 1)[-1]
            dbus = DBUS_RE.search(scope)
            tail = dbus.group(2) if dbus else tail
        else:
            # app-<launcher?>-<app id>-<pid or hash>: drop the trailing
            # instance number, then keep the last meaningful segment.
            rest = re.sub(r"^app-(?:lib-)?", "", scope)
            rest = re.sub(r"-[0-9a-f]{1,16}$", "", rest)
            parts = [p for p in rest.split("-") if p]
            tail = parts[-1] if parts else ""
        # A bare hash means the scope only names the launcher
        # (xdg-terminal-exec, gtk-launch): the app is not in the cgroup
        # name, so let the caller fall back to the process itself.
        if HASH_RE.match(tail) or tail.lower() in LAUNCHERS:
            return None
        name = clean_app_name(tail)
        if name:
            return "app:" + re.sub(r"[^A-Za-z0-9_.:\-]", "_", name)[:60]
        return None
    # No app scope (systemd user services, system slices): keep the unit.
    leaf = cgroup_line.rstrip("/").rsplit("/", 1)[-1]
    if not leaf or leaf == "/":
        return None
    if leaf == "init.scope":
        # PID 1's own scope catches every reparented orphan: too broad to
        # be a meaningful group. Fall back to the process tree instead.
        return None
    if leaf.endswith(".scope") or leaf.endswith(".service") or leaf.endswith(".slice"):
        name = clean_unit_name(leaf)
        if name:
            return "unit:" + re.sub(r"[^A-Za-z0-9_.:\-]", "_", name)[:60]
    return None


# Chromium/Electron child processes carry a --type= flag and name a suffix,
# not an app (Hermes --type=renderer still runs the "Hermes" binary).
TYPE_SUFFIXES = {
    "renderer", "gpu-process", "utility", "zygote", "broker", "cleanup",
    "asher", "watcher", "startup", "child", "plugin", "ppapi",
    "chrome-helper", "sandbox-helper", "command-buffer", "decoder",
    "command_buffer", "mjpeg", "crashpad", "back_forward_cache",
}


def base_name(proc):
    """Executable basename out of argv[0], whatever form it takes.

    argv[0] sometimes carries its own flags after the path
    (`.../Hermes --type=zygote`), so the path is cut at the first dash.
    """
    first = (proc.get("argv") or [""])[0]
    first = first.split(" --", 1)[0].split("--", 1)[0]
    base = os.path.basename(first.rstrip("/")) if "/" in first else first
    return clamp_text(base, NAME_LEN).strip()


def is_helper(proc):
    """A Chromium/Electron child: it names a --type= suffix, not an app.

    `Hermes --type=renderer` and `chrome_crashpad_handler --monitor-self-`
    are both helpers; the app is the process that started them.
    """
    name = base_name(proc).lower()
    argv = proc.get("argv") or []
    if any(a.startswith("--type=") for a in argv):
        return True
    # Some launches fold the flags into argv[0] itself.
    if "--type=" in name:
        return True
    if name in TYPE_SUFFIXES or name in LAUNCHERS:
        return True
    if "crashpad" in name:
        return True
    return "." in name and not name.endswith(".bin")


def app_exec_name(members):
    """The app worth naming a group after, from the processes themselves.

    Electron puts the real binary in argv[0] of the main process; its
    children (renderers, zygotes, crash handlers) only name a helper, so
    they are filtered out and the deepest child is the last resort.
    """
    real = [p for p in members if p["state"] != "Z"] or members
    main = [p for p in real if not is_helper(p)]
    if main:
        main.sort(key=lambda p: (-p.get("depth", 0), -len(p.get("argv") or []), p["pid"]))
        return clamp_text(base_name(main[0]), NAME_LEN)
    real.sort(key=lambda p: (-len(p.get("argv") or []), -p.get("depth", 0), p["pid"]))
    return clamp_text(base_name(real[0]), NAME_LEN)


def friendly_name(key):
    name = key.split(":", 1)[1] if ":" in key else key
    lowered = name.lower()
    for token, label in FRIENDLY.items():
        if lowered == token or lowered.startswith(token + "-"):
            return label
    return re.sub(r"[-_]+", " ", name).strip() or name


def snapshot(interval, runaway_samples):
    state = load_state(state_file())
    prev_cpu = state.get("cpu", {}) if isinstance(state.get("cpu"), dict) else {}
    prev_ids = state.get("ids", {}) if isinstance(state.get("ids"), dict) else {}
    prev_block = state.get("blocked", {}) if isinstance(state.get("blocked"), dict) else {}

    now = time.time()
    uptime = read_uptime()
    total_j, busy_j, mem_total, mem_avail, loadavg = read_totals()

    processes = []
    identities = {}
    try:
        entries = [int(name) for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        entries = []
    entries.sort()

    for pid in entries[:PID_CAP]:
        proc = read_stat_pid(pid)
        if proc is None:
            continue
        name, uid, rss_kib = read_status(pid)
        if uid is None:
            uid = -1
        proc["uid"] = uid
        proc["rss"] = (rss_kib or 0) * 1024
        if name:
            proc["comm"] = name[:NAME_LEN]
        proc["comm"] = clamp_text(proc["comm"], NAME_LEN)
        args = read_cmdline(pid) if proc["state"] != "Z" else []
        if not args:
            # An empty cmdline is a kernel thread: no address space, no
            # cmdline, not signal-able. It would only add noise here.
            if proc["state"] != "Z":
                continue
            proc["argv"] = [proc["comm"]]
        else:
            proc["argv"] = args
        proc["cgroup"] = read_cgroup(pid) if uid == os.getuid() else ""
        proc["startticks"] = int(proc.get("startticks") or 0)
        processes.append(proc)

        identities[str(pid)] = [proc["comm"], proc["startticks"], uid]

        # Per-PID CPU delta -> percentage of one core.
        key = str(pid)
        total_ticks = proc["utime"] + proc["stime"]
        old = prev_cpu.get(key)
        cpu = 0.0
        streak = 0
        if isinstance(old, list) and len(old) >= 2:
            try:
                delta = total_ticks - int(old[0])
                span = now - float(old[1])
            except (TypeError, ValueError):
                delta, span = 0, 0
            if span > 0.05 and delta >= 0:
                cpu = max(0.0, delta * 100.0 / (span * USER_HZ))
        streak = int(old[2]) if isinstance(old, list) and len(old) > 2 else 0
        if cpu >= RUNAWAY_CPU:
            streak += 1
        else:
            streak = 0
        proc["cpu"] = round(cpu, 1)
        proc["runaway"] = streak >= runaway_samples
        proc["_streak"] = streak

        # Blocked (D state) duration across consecutive samples.
        blocked = 0
        if proc["state"] == "D":
            try:
                blocked = int(prev_block.get(key, 0)) + 1
            except (TypeError, ValueError):
                blocked = 1
        proc["_blocked"] = blocked
        proc["blocked"] = blocked >= BLOCKED_SAMPLES

    by_pid = {p["pid"]: p for p in processes}

    # Depth in the process tree (bounded walk).
    for proc in processes:
        depth = 0
        cur = proc["pid"]
        seen = set()
        while depth < 64:
            parent = by_pid.get(cur)
            if parent is None or parent["ppid"] in (0, cur) or cur in seen:
                break
            seen.add(cur)
            cur = parent["ppid"]
            depth += 1
        proc["depth"] = depth

    my_uid = os.getuid()

    # ---- grouping -------------------------------------------------------
    groups = {}
    for proc in processes:
        key = app_key(proc["cgroup"]) if proc["cgroup"] else None
        if key is None:
            if proc["cgroup"]:
                # The scope only names the launcher (xdg-terminal-exec,
                # gtk-launch): keep that scope as the container instead of
                # merging every descendant under one parent process.
                leaf = proc["cgroup"].rstrip("/").rsplit("/", 1)[-1]
                leaf = re.sub(r"\.(scope|service|slice)$", "", leaf)
                leaf = leaf.replace("\\x2d", "-")
                if leaf:
                    key = "scope:" + re.sub(r"[^A-Za-z0-9_.:\-]", "_", leaf)[:60]
            if not key:
                parent = by_pid.get(proc["ppid"])
                if parent is None or parent["pid"] == proc["pid"] or parent["uid"] != proc["uid"]:
                    # Standalone process, or reparented to another user's tree.
                    key = "proc:%d:%s" % (proc["pid"], proc["comm"][:NAME_LEN])
                else:
                    key = "ppid:%d:%s" % (parent["pid"], parent["comm"][:NAME_LEN])
        group = groups.get(key)
        if group is None:
            group = {"key": key, "procs": [], "pids": [], "cpu": 0.0, "rss": 0,
                     "threads": 0, "zombies": 0, "blocked": 0, "runaways": 0,
                     "root": None}
            groups[key] = group
        group["procs"].append(proc)
        group["pids"].append(proc["pid"])
        group["cpu"] += proc["cpu"]
        group["rss"] += proc["rss"]
        group["threads"] += proc.get("threads") or 0
        if proc["state"] == "Z":
            group["zombies"] += 1
        if proc["blocked"]:
            group["blocked"] += 1
        if proc["runaway"]:
            group["runaways"] += 1

    uptime_ticks = uptime * 100.0
    rows = []
    for key, group in groups.items():
        members = group["procs"]
        members.sort(key=lambda p: (p["depth"], p["pid"]))
        root = members[0]
        group["root"] = root["pid"]

        # Every group here is already the collapsed row: the launcher, its
        # wrappers and the app all sit under one key.
        if key.startswith("app:") or key.startswith("unit:"):
            # The cgroup names the application or the systemd unit.
            label = friendly_name(key)
        else:
            # Only a launcher shows in the scope/parent key: name the group
            # after the executable the processes actually run.
            named = app_exec_name(members)
            if not named or named.lower() in LAUNCHERS:
                named = key.split(":", 1)[-1].split(":", 1)[-1]
            label = friendly_name("app:" + named)
        label = clamp_text(label, NAME_LEN)

        age = 0.0
        try:
            if uptime_ticks > root.get("startticks", 0):
                age = (uptime_ticks - root["startticks"]) / 100.0
        except TypeError:
            age = 0.0

        issues = []
        if group["runaways"]:
            issues.append("runaway")
        if group["blocked"]:
            issues.append("blocked")
        if group["zombies"]:
            issues.append("zombie")
        # A compositor or a service legitimately runs many processes, so
        # "spawning out of control" only counts for applications.
        if key.startswith("app:") and len(members) >= SWARM_PROCS:
            issues.append("swarm")
        # Only actionable states raise the alert. A zombie is already dead:
        # harmless, and it clears when its parent reaps it, so it stays info.
        severity = "critical" if ("runaway" in issues or "swarm" in issues) else (
            "warning" if "blocked" in issues else "ok")

        detailed = []
        for proc in sorted(members, key=lambda p: (-p["cpu"], -p["rss"], p["pid"]))[:MAX_PROCS_PER_GROUP]:
            preview = " ".join(proc.get("argv") or [proc["comm"]])
            detailed.append({
                "pid": proc["pid"],
                "comm": clamp_text(proc["comm"], NAME_LEN),
                "state": STATE_NAMES.get(proc["state"], "?"),
                "stateCode": proc["state"],
                "cpu": round(proc["cpu"], 1),
                "rss": round(proc["rss"] / (1024.0 * 1024.0), 1),
                "threads": proc.get("threads") or 0,
                "age": round(age if proc is root else max(0.0, (uptime_ticks - proc.get("startticks", 0)) / USER_HZ), 0),
                "preview": clamp_text(preview, PREVIEW_LEN),
                "mine": proc["uid"] == my_uid,
                "runaway": bool(proc["runaway"]),
            })

        rows.append({
            "id": key,
            "label": label,
            "rootPid": root["pid"],
            "pids": group["pids"],
            "nproc": len(members),
            "cpu": round(group["cpu"], 1),
            "mem": round(group["rss"] / (1024.0 * 1024.0), 1),
            "threads": group["threads"],
            "age": round(age),
            "severity": severity,
            "issues": issues,
            "procs": detailed,
        })

    rank = {"critical": 0, "warning": 1, "ok": 2}
    rows.sort(key=lambda r: (rank.get(r["severity"], 2), -r["cpu"], -r["mem"], r["label"].lower()))
    rows = rows[:MAX_GROUPS]

    totals = {
        "procs": len(processes),
        "cpu": round(sum(p["cpu"] for p in processes), 1),
        "mem": round(sum(p["rss"] for p in processes) / (1024.0 * 1024.0)),
        "memTotal": mem_total,
        "memAvail": mem_avail,
        "load": round(loadavg, 2),
        "groups": len(rows),
    }
    issue_counts = {
        "runaway": sum(1 for r in rows if "runaway" in r["issues"]),
        "blocked": sum(1 for r in rows if "blocked" in r["issues"]),
        "zombie": sum(1 for r in rows if "zombie" in r["issues"]),
        "swarm": sum(1 for r in rows if "swarm" in r["issues"]),
    }
    issue_counts["total"] = sum(1 for r in rows if r["severity"] != "ok")

    cpu_state = {}
    blocked_state = {}
    for proc in processes:
        key = str(proc["pid"])
        cpu_state[key] = [proc["utime"] + proc["stime"], now, proc.get("_streak", 0)]
        if proc.get("_blocked"):
            blocked_state[key] = proc["_blocked"]

    path = state_file()
    if path:
        save_state(path, {
            "version": STATE_VERSION, "ts": now, "cpu": cpu_state,
            "ids": identities, "blocked": blocked_state,
        })

    return {"ts": now, "interval": interval, "totals": totals,
            "issues": issue_counts, "groups": rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Process Pulse snapshot")
    sub = parser.add_subparsers(dest="cmd")
    snap = sub.add_parser("snapshot")
    snap.add_argument("--interval", type=float, default=2.0)
    snap.add_argument("--runaway-samples", type=int, default=5)
    parser.parse_args(argv)

    args = parser.parse_args(argv)
    if args.cmd != "snapshot":
        parser.print_help()
        return 2

    interval = max(0.5, min(60.0, args.interval or 2.0))
    runaway = max(2, min(200, args.runaway_samples or 5))
    try:
        payload = snapshot(interval, runaway)
    except Exception as exc:  # the panel must never see a traceback
        payload = {"ts": time.time(), "totals": {}, "issues": {"total": 0},
                   "groups": [], "error": clamp_text(exc, 200)}
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
