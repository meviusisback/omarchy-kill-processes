#!/usr/bin/env python3
"""Unit tests for probe.py and kill.py — pure fixtures, no /proc, no network.

Run:  python3 tests/test_probe.py
Exits non-zero on failure.
"""

import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import kill as killmod  # noqa: E402
import probe as probemod  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s %s" % (name, extra))


# ----------------------------------------------------------------- fixtures
def make_proc(pid, comm="prog", state="S", uid=None, ppid=1, cpu_ticks=100,
              argv=None, cgroup="", threads=1, start=12345, depth=1):
    if uid is None:
        uid = os.getuid()
    return {
        "pid": pid, "comm": comm, "state": state, "uid": uid, "ppid": ppid,
        "pgrp": ppid, "session": ppid, "utime": cpu_ticks, "stime": 0,
        "startticks": start, "nice": 0, "threads": threads,
        "argv": argv if argv is not None else [comm], "cgroup": cgroup,
        "rss": 10 * 1024 * 1024, "cpu": 0.0, "depth": depth,
        "runaway": False, "blocked": False, "_streak": 0, "_blocked": 0,
    }


def with_state_tmp():
    tmp = tempfile.mkdtemp(prefix="killprocs-test-")
    os.environ["XDG_STATE_HOME"] = tmp
    return tmp


# ------------------------------------------------------------- app_key tests
def test_app_key():
    print("== app_key ==")
    # systemd unit fallback
    k = probemod.app_key("0::/user.slice/user-1000.slice/user@1000.service/app.slice/hermes-gateway.service")
    check("unit hermes gateway", k == "unit:hermes_gateway", repr(k))

    # hyprland app scope with launcher + escaped hyphen: launcher tail -> None
    k = probemod.app_key("0::/user.slice/app.slice/app-Hyprland-gtk\\x2dlaunch-2550f89f.scope")
    check("gtk-launch scope -> None (launcher only)", k is None, repr(k))

    # hyprland scope with a real app id
    k = probemod.app_key("0::/app.slice/app-com.nousresearch.hermes-3547524.scope")
    check("hermes app scope", k == "app:hermes", repr(k))

    # electron/chrome style scope (dot id)
    k = probemod.app_key("0::/app.slice/app-org.chromium.Chromium-1723155.scope")
    check("chromium scope -> Chromium", k == "app:Chromium", repr(k))

    # plain session scope (not app/unit) -> None
    k = probemod.app_key("0::/user.slice/user-1000.slice/user@1000.service/init.scope")
    check("init.scope -> None", k is None, repr(k))


def test_names():
    print("== names ==")
    check("clean_app_name desktop",
          probemod.clean_app_name("org.gnome.Nautilus.desktop") == "Nautilus")
    check("clean_unit_name service",
          probemod.clean_unit_name("hermes-gateway.service") == "hermes gateway")
    check("instance hash stripped",
          probemod.clean_unit_name("hermes-worker-proc-891226b80642") == "hermes worker proc")
    check("clean_app_name @ instance",
          probemod.clean_app_name("wayland-wm@hyprland.desktop") == "hyprland")
    check("clamp removes control chars",
          probemod.clamp_text("a\x00\x1b[31mb\nc", 100) == "a[31mbc")
    check("clamp caps length", len(probemod.clamp_text("x" * 500, 50)) <= 50)


def test_parse_stat():
    print("== parse_stat ==")
    line = "123 (comm with (parens)) S 100 101 102 0 -1 0 5 0 0 0 10 20 0 0 20 7 1 0 99 0"
    comm, f = probemod.parse_stat(line)
    check("comm keeps parens", comm == "comm with (parens)", repr(comm))
    check("state field", f[0] == "S")
    check("ppid field", f[1] == "100")
    check("utime f11", f[11] == "10")
    check("stime f12", f[12] == "20")
    check("threads f17", f[17] == "1")
    check("startticks f19", f[19] == "99")
    check("garbage rejected", probemod.parse_stat("not a stat line") is None)
    check("short tail rejected", probemod.parse_stat("1 (x) S 1 2") is None)


# ----------------------------------------------------------- snapshot tests
def run_snapshot(monkey_procs, interval=2.0, runaway=5):
    """Snapshot with patched readers. monkey_procs: list of proc dicts."""
    by_pid = {p["pid"]: p for p in monkey_procs}

    def fake_stat_pid(pid):
        p = by_pid.get(pid)
        if not p:
            return None
        return {"pid": pid, "ppid": p["ppid"], "pgrp": p["pgrp"], "session": p["session"],
                "utime": p["utime"], "stime": p["stime"], "startticks": p["startticks"],
                "nice": 0, "threads": p["threads"], "state": p["state"], "comm": p["comm"]}

    def fake_status(pid):
        p = by_pid.get(pid, {})
        return p.get("comm"), p.get("uid", 1000), 10240

    def fake_cmdline(pid):
        return by_pid.get(pid, {}).get("argv") or []

    def fake_cgroup(pid):
        return by_pid.get(pid, {}).get("cgroup", "")

    orig = (probemod.read_stat_pid, probemod.read_status,
            probemod.read_cmdline, probemod.read_cgroup,
            probemod.read_uptime, probemod.read_totals)
    clock = getattr(run_snapshot, "_clock", [1700000000.0])
    clock[0] += interval                      # time passes between samples
    run_snapshot._clock = clock
    orig_time = probemod.time.time
    probemod.time.time = lambda: clock[0]
    orig_listdir = probemod.os.listdir
    probemod.os.listdir = lambda _p: [str(p["pid"]) for p in monkey_procs]
    probemod.read_stat_pid = fake_stat_pid
    probemod.read_status = fake_status
    probemod.read_cmdline = fake_cmdline
    probemod.read_cgroup = fake_cgroup
    probemod.read_uptime = lambda: 100000.0
    probemod.read_totals = lambda: (100000, 50000, 16 * 1024 ** 3, 8 * 1024 ** 3, 0.5)
    try:
        return probemod.snapshot(interval, runaway)
    finally:
        (probemod.read_stat_pid, probemod.read_status,
         probemod.read_cmdline, probemod.read_cgroup,
         probemod.read_uptime, probemod.read_totals) = orig
        probemod.os.listdir = orig_listdir
        probemod.time.time = orig_time


def test_snapshot_grouping():
    print("== snapshot: grouping ==")
    with_state_tmp()
    CG = "0::/user.slice/app.slice/app-org.firefox-4242.scope"
    procs = [
        make_proc(10, "firefox", cgroup=CG, argv=["/usr/lib/firefox/firefox"], depth=1),
        make_proc(11, "firefox", cgroup=CG, argv=["/usr/lib/firefox/firefox", "-contentproc"],
                  ppid=10, depth=2),
        make_proc(12, "sleep", cgroup="0::/user.slice/xfce4/terminal.scope",
                  argv=["sleep", "300"], ppid=1, depth=1),
    ]
    d = run_snapshot(procs)
    check("no error", not d.get("error"), d.get("error"))
    labels = {g["label"]: g for g in d["groups"]}
    check("firefox grouped as one", "Firefox" in labels, list(labels))
    ff = labels.get("Firefox")
    if ff:
        check("firefox nproc=2", ff["nproc"] == 2, ff["nproc"])
        check("firefox pids", sorted(ff["pids"]) == [10, 11], ff["pids"])
        check("firefox sev ok", ff["severity"] == "ok", ff["severity"])
    check("cgroup label present", "terminal" in labels, list(labels))
    check("totals present", d["totals"]["procs"] == 3, d["totals"])


def test_snapshot_cpu_and_runaway():
    print("== snapshot: cpu delta + runaway ==")
    with_state_tmp()
    CG = "0::/app.slice/app-org.foo.App-1.scope"
    procs = [make_proc(20, "app", cgroup=CG, argv=["/usr/bin/app"], cpu_ticks=1000)]
    run_snapshot(procs)                      # sample 1: baseline
    # 5 samples of +200 ticks over 2 s => 100% of a core each sample
    d = None
    for i in range(5):
        procs[0]["utime"] += 200
        d = run_snapshot(procs)
    g = d["groups"][0]
    p = g["procs"][0]
    check("cpu about 100", 90 <= p["cpu"] <= 110, p["cpu"])
    check("runaway flagged", p["runaway"] is True, p)
    check("group critical", g["severity"] == "critical", g["severity"])
    check("issues has runaway", "runaway" in g["issues"], g["issues"])
    check("issues.total counted", d["issues"]["runaway"] == 1 and d["issues"]["total"] == 1, d["issues"])


def test_snapshot_zombie_and_kernel():
    print("== snapshot: zombies + kernel threads ==")
    with_state_tmp()
    procs = [
        make_proc(30, "defunct", state="Z", argv=["defunct"]),
        make_proc(31, "kworker", argv=[], uid=0),          # kernel thread: no cmdline
        make_proc(32, "ok", argv=["ok"]),
    ]
    d = run_snapshot(procs)
    labels = [g["label"] for g in d["groups"]]
    check("zombie issue counted", d["issues"]["zombie"] == 1, d["issues"])
    zg = [g for g in d["groups"] if "zombie" in g["issues"]]
    check("zombie group flagged", len(zg) == 1, labels)
    check("kernel thread skipped", d["totals"]["procs"] == 2, d["totals"])
    check("no kworker label", not any("kworker" in l for l in labels), labels)


def test_snapshot_swarm_only_apps():
    print("== snapshot: swarm only for app groups ==")
    with_state_tmp()
    procs = [make_proc(100 + i, "many", cgroup="0::/app.slice/app-org.big-1.scope",
                       argv=["many"], depth=1) for i in range(45)]
    d = run_snapshot(procs)
    g = d["groups"][0]
    check("app swarm flagged", "swarm" in g["issues"], g["issues"])
    check("app swarm critical", g["severity"] == "critical", g["severity"])

    procs = [make_proc(300 + i, "unit", cgroup="0::/user.slice/session-9.scope",
                       argv=["unit"]) for i in range(45)]
    d = run_snapshot(procs)
    g = d["groups"][0]
    check("unit swarm NOT flagged", "swarm" not in g["issues"], g["issues"])


def test_snapshot_caps():
    print("== snapshot: output caps ==")
    with_state_tmp()
    procs = []
    for i in range(70):
        procs.append(make_proc(500 + i, "app%d" % i,
                               cgroup="0::/app.slice/app-org.app%d-1.scope" % i,
                               argv=["app%d" % i]))
    d = run_snapshot(procs)
    check("groups capped at 60", len(d["groups"]) == 60, len(d["groups"]))


# ---------------------------------------------------------------- kill tests
def test_check_pid():
    print("== kill: check_pid ==")
    snap = {"55": ["sleep", 1111, 1000]}
    uid = 1000
    orig_live = killmod.live_identity
    killmod.live_identity = lambda pid: ("sleep", 1111, 1000, "S")
    try:
        ident_ok, reason = killmod.check_pid(55, snap, uid)
        check("happy path", ident_ok is not None and reason is None, reason)
    finally:
        killmod.live_identity = orig_live
    _, r = killmod.check_pid(1, snap, uid)
    check("reserved", r == "reserved", r)
    _, r = killmod.check_pid(400000000, snap, uid)
    check("out-of-range", r == "out-of-range", r)
    _, r = killmod.check_pid(56, snap, uid)
    check("not-in-snapshot", r == "not-in-snapshot", r)

    orig = killmod.live_identity
    killmod.live_identity = lambda pid: None
    try:  # noqa: SIM105
        _, r = killmod.check_pid(55, snap, uid)
        check("gone", r == "gone", r)
        # recycled: same pid, different start time
        killmod.live_identity = lambda pid: ("sleep", 2222, 1000, "S")
        _, r = killmod.check_pid(55, snap, uid)
        check("recycled-pid (starttime)", r == "recycled-pid", r)
        # same start, different comm
        killmod.live_identity = lambda pid: ("other", 1111, 1000, "S")
        _, r = killmod.check_pid(55, snap, uid)
        check("recycled-pid (comm)", r == "recycled-pid", r)
        # other user's process: snapshot and live agree, caller differs
        other = 0 if os.getuid() != 0 else 1000
        snap_other = {"55": ["sleep", 1111, other]}
        killmod.live_identity = lambda pid: ("sleep", 1111, other, "S")
        _, r = killmod.check_pid(55, snap_other, os.getuid())
        check("not-yours", r == "not-yours", r)
        # zombie
        killmod.live_identity = lambda pid: ("sleep", 1111, 1000, "Z")
        _, r = killmod.check_pid(55, snap, uid)
        check("zombie refused", r == "zombie", r)
    finally:
        killmod.live_identity = orig


def test_send_and_main():
    print("== kill: send + main flow ==")
    calls = []
    orig_run, orig_isfile = killmod.subprocess.run, killmod.os.path.isfile
    killmod.os.path.isfile = lambda p: True

    class FakeResult:
        returncode = 0

    def fake_run(argv, **kw):
        calls.append(argv)
        assert kw.get("shell") is not True
        return FakeResult()

    killmod.subprocess.run = fake_run

    tmp = with_state_tmp()
    state_path = killmod.state_file()
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    me = os.getuid()
    orig_live2 = killmod.live_identity
    orig_sleep2 = killmod.time.sleep
    # alive while validating, dead once the grace period has elapsed
    phase2 = {"grace_done": False}
    killmod.live_identity = lambda pid: None if phase2["grace_done"] else ("sleep", 1111, me, "S")
    killmod.time.sleep = lambda t: phase2.update(grace_done=True)
    with open(state_path, "w") as fh:
        json.dump({"version": 1, "ts": time.time(), "ids": {"55": ["sleep", 1111, me]}}, fh)

    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = killmod.main(["--sig", "term", "--pid", "55"])
        out = json.loads(buf.getvalue().strip().splitlines()[-1])
        check("main rc 0", rc == 0, rc)
        check("payload ok", out.get("ok") is True, out)
        check("term accepted", out.get("term") == [55], out)
        check("argv is a list (no shell)", calls and isinstance(calls[0], list), calls[:1])
        check("kill binary absolute", calls and calls[0][0] == killmod.KILL_BIN, calls[:1])
        check("signal arg", calls and calls[0][1] == "-TERM", calls[:1])
        check("grace reported", out.get("grace") == killmod.GRACE_S, out.get("grace"))

        # refusal path: pid not in snapshot
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            rc = killmod.main(["--sig", "kill", "--pid", "999"])
        out2 = json.loads(buf2.getvalue().strip().splitlines()[-1])
        check("refusal reported", out2.get("refused", {}).get("999") == "not-in-snapshot", out2)

        # bad signal name
        buf3 = io.StringIO()
        with redirect_stdout(buf3):
            rc = killmod.main(["--sig", "HUG", "--pid", "55"])
        out3 = json.loads(buf3.getvalue().strip().splitlines()[-1])
        check("unknown signal rejected", out3.get("error") == "unknown-signal", out3)
    finally:
        killmod.subprocess.run, killmod.os.path.isfile = orig_run, orig_isfile
        killmod.live_identity = orig_live2
        killmod.time.sleep = orig_sleep2


def test_still_running_zombie():
    print("== kill: zombie is not a survivor (main-level) ==")
    # ensure main() treats a zombie as dead: patched live_identity -> Z
    calls = []
    orig_run, orig_isfile, orig_live = killmod.subprocess.run, killmod.os.path.isfile, killmod.live_identity
    killmod.os.path.isfile = lambda p: True

    class FakeResult:
        returncode = 0
    killmod.subprocess.run = lambda argv, **kw: (calls.append(argv), FakeResult())[1]

    with_state_tmp()
    os.makedirs(os.path.dirname(killmod.state_file()), exist_ok=True)
    me2 = os.getuid()
    with open(killmod.state_file(), "w") as fh:
        json.dump({"version": 1, "ts": time.time(), "ids": {"77": ["x", 1, me2]}}, fh)

    states = {"phase": 0}
    def fake_live(pid):
        # first call (validation): alive S; after grace: Z (dead-unreaped)
        return ("x", 1, me2, "S") if states["phase"] == 0 else ("x", 1, me2, "Z")
    orig_sleep = killmod.time.sleep
    def fake_sleep(t):
        states["phase"] = 1
    killmod.time.sleep = fake_sleep
    killmod.live_identity = fake_live
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = killmod.main(["--sig", "term", "--pid", "77"])
        out = json.loads(buf.getvalue().strip().splitlines[-1] if False else buf.getvalue().strip().splitlines()[-1])
        check("zombie not force-killed", out.get("killed") == [], out)
        check("counted as term", out.get("term") == [77], out)
        # exactly one signal sent (TERM only)
        check("only TERM sent", len(calls) == 1, calls)
    finally:
        killmod.subprocess.run, killmod.os.path.isfile = orig_run, orig_isfile
        killmod.live_identity = orig_live
        killmod.time.sleep = orig_sleep


def main():
    tests = [
        test_app_key, test_names, test_parse_stat,
        test_snapshot_grouping, test_snapshot_cpu_and_runaway,
        test_snapshot_zombie_and_kernel, test_snapshot_swarm_only_apps,
        test_snapshot_caps, test_check_pid, test_send_and_main,
        test_still_running_zombie,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            global FAIL
            FAIL += 1
            print("FAIL  %s raised %r" % (t.__name__, e))
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
