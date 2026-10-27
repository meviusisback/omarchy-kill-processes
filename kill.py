#!/usr/bin/env python3
"""Process Pulse - signal a process, only after checking it is still the one.

Reads the PIDs from argv (never from a shell), validates every one of them
against the identity snapshot the panel took earlier (command name, start
time, owner) and only then sends a signal. A recycled PID is refused, so
clicking Kill on "firefox" can never hit whatever now owns that number.

Sequence: SIGTERM, two seconds of grace, then SIGKILL on whatever survived.

Usage:
  kill.py --sig term  --pid 1234 --pid 1235 ...
  kill.py --sig kill  --pid 1234
"""

import json
import os
import subprocess
import sys
import time

STATE_VERSION = 1
STATE_MAX_AGE_S = 300.0
KILL_BIN = "/usr/bin/kill"
MAX_PIDS = 128
GRACE_S = 2.0

# The signal the user asked for, then the one used to finish the job.
SIGNALS = {"term": {"soft": "TERM", "hard": "KILL"},
           "kill": {"soft": "KILL", "hard": "KILL"}}


def state_file():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "omarchy-killprocs", "state.json")


def load_state(path):
    try:
        with open(path, "rb") as handle:
            raw = handle.read(4 * 1024 * 1024)
        data = json.loads(raw.decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        return None
    if not isinstance(data.get("ids"), dict):
        return None
    try:
        if abs(float(data.get("ts", 0)) - time.time()) > STATE_MAX_AGE_S:
            return None
    except (TypeError, ValueError):
        return None
    return data


def live_identity(pid):
    """(comm, starttime, uid, state) straight from /proc right now."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as handle:
            text = handle.read(4096).decode("utf-8", "replace")
    except OSError:
        return None
    left = text.find("(")
    right = text.rfind(")")
    if left < 0 or right < left:
        return None
    comm = text[left + 1:right]
    fields = text[right + 1:].split()
    if len(fields) < 20:
        return None
    uid = -1
    try:
        with open("/proc/%d/status" % pid, "rb") as handle:
            for line in handle.read(4096).decode("utf-8", "replace").split("\n"):
                if line.startswith("Uid:"):
                    uid = int(line.split()[1])
                    break
    except (OSError, ValueError, IndexError):
        pass
    try:
        return comm, int(fields[19]), uid, fields[0]
    except (ValueError, IndexError):
        return None


def check_pid(pid, snapshot, my_uid):
    """Refuse anything that is not the process the panel showed."""
    if pid <= 2:
        return None, "reserved"
    if pid > 4194304:
        return None, "out-of-range"
    # The snapshot is JSON, so its keys come back as strings.
    expected = snapshot.get(str(pid))
    if expected is None:
        return None, "not-in-snapshot"

    now = live_identity(pid)
    if now is None:
        return None, "gone"
    comm, startticks, uid, state = now

    if isinstance(expected, list) and len(expected) >= 2:
        exp_comm = str(expected[0])
        exp_start = expected[1]
        exp_uid = expected[2] if len(expected) > 2 else -1
        if comm != exp_comm:
            return None, "recycled-pid"
        try:
            if int(exp_start) != startticks:
                return None, "recycled-pid"
        except (TypeError, ValueError):
            return None, "recycled-pid"
        if int(exp_uid) != uid:
            return None, "recycled-pid"

    if uid != my_uid:
        return None, "not-yours"
    if state == "Z":
        return None, "zombie"
    return (comm, startticks, uid, state), None


def send(pid, name):
    """One signal, argv only, no shell. False when the process is gone."""
    if not os.path.isfile(KILL_BIN):
        return False
    try:
        result = subprocess.run([KILL_BIN, "-" + name, str(pid)],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    sig = "term"
    pids = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--sig", "-s") and i + 1 < len(argv):
            sig = argv[i + 1].lower()
            i += 2
            continue
        if arg in ("--pid", "-p") and i + 1 < len(argv):
            pids.append(argv[i + 1])
            i += 2
            continue
        if arg.startswith("--pid="):
            pids.append(arg.split("=", 1)[1])
            i += 1
            continue
        if arg.startswith("--sig="):
            sig = arg.split("=", 1)[1].lower()
            i += 1
            continue
        i += 1

    if sig not in SIGNALS:
        sys.stdout.write(json.dumps({"ok": False, "error": "unknown-signal"}) + "\n")
        return 2
    if not pids:
        sys.stdout.write(json.dumps({"ok": False, "error": "no-pids"}) + "\n")
        return 2

    state = load_state(state_file())
    if state is None:
        sys.stdout.write(json.dumps({"ok": False, "error": "no-snapshot"}) + "\n")
        return 1

    my_uid = os.getuid()
    accepted, refused = [], {}
    for raw in pids[:MAX_PIDS]:
        try:
            pid = int(str(raw).strip())
        except ValueError:
            refused[str(raw)[:16]] = "not-a-pid"
            continue
        ident, reason = check_pid(pid, state["ids"], my_uid)
        if ident is None:
            refused[str(pid)] = reason
        else:
            accepted.append(pid)

    soft, hard = SIGNALS[sig]["soft"], SIGNALS[sig]["hard"]
    grace = GRACE_S if hard != soft else 0
    signaled = [pid for pid in accepted if send(pid, soft)]
    refused.update({str(pid): "signal-failed" for pid in accepted if pid not in signaled})

    killed = []
    if hard != soft and signaled:
        time.sleep(GRACE_S)

        def still_running(pid):
            """Dead is dead: a zombie is an unreaped corpse, not a survivor."""
            ident = live_identity(pid)
            return ident is not None and ident[3] != "Z"

        survivors = [pid for pid in signaled if still_running(pid)]
        killed = [pid for pid in survivors if send(pid, hard)]
        signaled = [pid for pid in signaled if pid not in survivors]

    payload = {
        "ok": True, "sig": sig, "soft": soft, "hard": hard if hard != soft else None,
        "term": sorted(signaled), "killed": sorted(killed), "refused": refused,
        "grace": grace,
    }
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
