#!/usr/bin/env python3
"""
Pulse cron dispatcher. Scans brain/.pulse/crons/*.md, matches schedules
against current time, and runs due crons through the vendor-neutral runner
(g-env's bin/g-agent-run). Replaces per-cron systemd timers: the schedule
lives in frontmatter.

Frontmatter: name, schedule, runner (agent; legacy "claude" means the same),
role (g-agent-run role: bulk|verify|gather|judge|review, never a model name),
enabled, optional connectors: true (Claude-only account MCP connectors), optional
gate: a shell command run first with no model (exit 0 = there is work, exit 3 = no work
-> result noop, provider "gate", zero tokens; anything else = fail; its stdout is appended
to the prompt, so a gate can also do the fetching), optional workdir:
(default brain/.pulse; a scratch dir keeps its AGENTS.md out of every turn's context).
Every prompt gets a job contract appended: the job must end with one line
`RESULT: ok|noop|fail - <sentence>`. Each run appends one row to
state/cron-runs.jsonl; fail/missing/non-zero pages the alert spine once per
failure streak and the streak clears on the next ok/noop.

    pulse-cron-dispatch.py              run whatever is due (the timer path)
    pulse-cron-dispatch.py --list       show every cron file and its settings
    pulse-cron-dispatch.py --run NAME   run one job now, schedule ignored
"""
import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# The brain, brain-tools and g-env sit beside this checkout. BRAIN_ROOT (the unit
# sets it), BRAIN_TOOLS, G_ENV and G_AGENT_RUN override that.
WORKSPACE = Path(__file__).resolve().parents[2]
PULSE_DIR = Path(os.environ.get("BRAIN_ROOT") or WORKSPACE / "brain") / ".pulse"
CRONS_DIR = PULSE_DIR / "crons"
STATE_DIR = PULSE_DIR / "state"
RUNS_FILE = STATE_DIR / "cron-runs.json"
LOG_FILE  = STATE_DIR / "cron.log"
LOCK_FILE = STATE_DIR / "dispatch.lock"
RUNS_LOG  = STATE_DIR / "cron-runs.jsonl"
STREAKS_FILE = STATE_DIR / "cron-streaks.json"
G_ENV = os.environ.get("G_ENV") or os.environ.get("GENV_DIR") or next(
    (str(p) for p in (WORKSPACE / "g-env", WORKSPACE / "genv") if p.exists()), str(WORKSPACE / "g-env"))
AGENT_RUN = os.environ.get("G_AGENT_RUN") or str(Path(G_ENV) / "bin/g-agent-run")
BRAIN_TOOLS = Path(os.environ.get("BRAIN_TOOLS") or WORKSPACE / "brain-tools")
RUNNERS = ("agent", "claude")   # "claude" is the legacy spelling of "agent"
GATE_TIMEOUT_S = 120
GATE_NO_WORK = 3
GATE_OUT_CAP = 40000           # chars of gate stdout handed to the model
JOB_TIMEOUT_S = 3600            # g-agent-run bounds each provider; this bounds the whole fallback chain

MAX_LOOKBACK_HOURS = 25


def acquire_lock():
    lf = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)
    return lf


def parse_frontmatter(text):
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    fm = {}
    for line in lines[1:end]:
        if ":" in line:
            k, _, v = line.partition(":")
            # strip inline comments after #
            v = v.split("#")[0].strip().strip('"').strip("'")
            fm[k.strip()] = v
    body = "\n".join(lines[end + 1:]).strip()
    return fm, body


def field_matches(expr, value, min_val, max_val):
    """Return True if cron field expression matches integer value."""
    if expr == "*":
        return True
    result = set()
    for part in expr.split(","):
        if "/" in part:
            base, step = part.split("/", 1)
            step = int(step)
            if base == "*":
                lo, hi = min_val, max_val
            elif "-" in base:
                lo, hi = map(int, base.split("-", 1))
            else:
                lo = int(base)
                hi = max_val
            result.update(range(lo, hi + 1, step))
        elif "-" in part:
            lo, hi = map(int, part.split("-", 1))
            result.update(range(lo, hi + 1))
        else:
            result.add(int(part))
    return value in result


def schedule_matches(schedule, dt):
    """Return True if 5-field cron schedule matches dt (minute precision)."""
    parts = schedule.split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = parts
    return (
        field_matches(minute, dt.minute, 0, 59) and
        field_matches(hour,   dt.hour,   0, 23) and
        field_matches(dom,    dt.day,    1, 31) and
        field_matches(month,  dt.month,  1, 12) and
        field_matches(dow,    dt.weekday() % 7, 0, 6)  # 0=Mon python -> 0=Sun cron not handled; good enough
    )


def is_due(schedule, last_run_iso, now):
    """Return True if any minute (last_run, now] matches schedule."""
    try:
        last = datetime.fromisoformat(last_run_iso).replace(tzinfo=timezone.utc)
    except Exception:
        return False
    cutoff = now - timedelta(hours=MAX_LOOKBACK_HOURS)
    start = max(last, cutoff)
    # iterate minute by minute from start+1m to now inclusive
    t = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
    end = now.replace(second=0, microsecond=0)
    while t <= end:
        if schedule_matches(schedule, t):
            return True
        t += timedelta(minutes=1)
    return False


def strip_frontmatter(text):
    _, body = parse_frontmatter(text)
    return body


JOB_CONTRACT = """

---
JOB CONTRACT (from the scheduler, applies to this whole task): finish with exactly
one last line `RESULT: ok|noop|fail - <one sentence>`. ok = you did the work,
noop = nothing needed doing, fail = you could not do it. If a source, tool or
connector this task needs is unreachable or unauthorised, that is fail, not ok:
never report ok for work you did not do, and never ask questions or offer options,
nobody is reading. No greeting."""
RESULT_RE = re.compile(r"^RESULT:\s*(ok|noop|fail)\s*-\s*(.*\S)\s*$", re.I)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_result(output):
    """Last RESULT line wins; no line at all is "missing", which pages like fail."""
    for line in reversed(output.strip().splitlines()):
        m = RESULT_RE.match(line.strip().strip("`*"))
        if m:
            return m.group(1).lower(), m.group(2)
    tail = " ".join(output.split())[-200:]
    return "missing", tail or "no output"


def run_gate(cmd):
    """Returns (verdict, text, stdout): verdict is 'work', 'nowork' or 'error'."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=GATE_TIMEOUT_S, cwd=str(PULSE_DIR))
    except subprocess.TimeoutExpired:
        return "error", f"gate timed out after {GATE_TIMEOUT_S}s", ""
    text = " ".join((r.stdout or r.stderr).split())[-200:]
    if r.returncode == 0:
        return "work", text, r.stdout
    if r.returncode == GATE_NO_WORK:
        return "nowork", text or "gate: no work", ""
    return "error", f"gate exit {r.returncode}: {text}", ""


def run_cron(name, body, role, connectors=False, gate="", workdir=""):
    """Run one job through g-agent-run. Returns the run-row dict (without name/role)."""
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    if gate:
        verdict, text, gate_out = run_gate(gate)
        if verdict != "work":
            return {"started": started.isoformat(timespec="seconds"), "finished": now_iso(),
                    "duration_s": int(time.monotonic() - t0), "exit": 0 if verdict == "nowork" else 1,
                    "provider": "gate", "model": "", "result": "noop" if verdict == "nowork" else "fail", "summary": text}
    fd, prompt_file = tempfile.mkstemp(prefix="pulse-cron-", suffix=".txt")
    meta_file = prompt_file + ".meta"
    with os.fdopen(fd, "w") as f:
        f.write(body + (f"\n\nGATE OUTPUT (the gate command already ran; this is its stdout):\n{gate_out[:GATE_OUT_CAP]}" if gate else "")
                + JOB_CONTRACT)
    argv = [AGENT_RUN, "--role", role, "--prompt-file", prompt_file, "--workdir", workdir or str(PULSE_DIR),
            "--tools", "--sandbox", "danger-full-access", "--meta", meta_file]
    if connectors:
        argv.append("--connectors")
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=JOB_TIMEOUT_S, cwd=str(PULSE_DIR))
        exit_code, output, err = r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        exit_code, output, err = -1, "", "timeout"
    except OSError as e:
        exit_code, output, err = 127, "", f"cannot start {AGENT_RUN}: {e}"
    provider = ""
    try:
        provider = json.load(open(meta_file)).get("provider") or ""
    except (OSError, ValueError):
        pass
    for p in (prompt_file, meta_file):
        try:
            os.unlink(p)
        except OSError:
            pass
    if exit_code == 0:
        result, summary = parse_result(output)
    else:
        result, summary = "missing", " ".join((err or output).split())[-300:] or f"exit {exit_code}"
    vendor, _, model = provider.partition(":")
    return {
        "started": started.isoformat(timespec="seconds"), "finished": now_iso(),
        "duration_s": int(time.monotonic() - t0), "exit": exit_code,
        "provider": vendor, "model": model, "result": result, "summary": summary,
    }


def page(level, msg, key=None, clear=False):
    """Go through the alert spine's own send path; its venv has the deps system python lacks."""
    if clear:
        code = ("import sys, brain_alerts; brain_alerts.clear(sys.argv[1])", key)
    else:
        code = ("import sys, brain_alerts; print(brain_alerts.send(sys.argv[1], 'pulse-cron', sys.argv[2], dedup_key=sys.argv[3]))",
                level, msg, key)
    try:
        r = subprocess.run([str(BRAIN_TOOLS / ".venv/bin/python"), "-c", *code], cwd=str(BRAIN_TOOLS),
                           capture_output=True, text=True, timeout=60)
        sent = r.returncode == 0 and "'ok': True" in r.stdout
        detail = " ".join((r.stdout + r.stderr).split())[-200:]
    except (OSError, subprocess.TimeoutExpired) as e:
        sent, detail = False, str(e)
    if not clear:  # leave evidence: the page used to vanish without a trace
        with open(LOG_FILE, "a") as f:
            f.write(f"{now_iso()} page key={key} sent={sent} detail={detail!r}\n")
    return sent or clear


def record_run(name, role, row):
    row = {"name": name, "role": role, **row}
    with open(RUNS_LOG, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    with open(LOG_FILE, "a") as f:
        f.write(f"{row['started']} name={name} role={role} provider={row['provider']}:{row['model']} "
                f"exit={row['exit']} duration={row['duration_s']}s result={row['result']} summary={row['summary']!r}\n")
    # One page per job per failure streak (not per 5-minute tick), cleared by the next ok/noop.
    try:
        streaks = json.load(open(STREAKS_FILE))
    except (OSError, ValueError):
        streaks = {}
    bad = row["result"] in ("fail", "missing") or row["exit"] != 0
    key = f"pulse-cron:{name}"
    if bad and name not in streaks:
        # A page that did not go out leaves no streak entry, so the next failure tries again.
        if page("error", f"{name} {row['result']} (exit {row['exit']}, {role}): {row['summary'][:200]}", key):
            streaks[name] = row["finished"]
    elif not bad and name in streaks:
        del streaks[name]
        page("info", "", key, clear=True)
    with open(STREAKS_FILE, "w") as f:
        json.dump(streaks, f, indent=2)


def load_crons():
    """Every cron file as a dict of settings; template files (leading _) are skipped."""
    out = []
    for md_file in sorted(CRONS_DIR.glob("*.md")):
        if md_file.name.startswith("_"):
            continue
        fm, body = parse_frontmatter(md_file.read_text())
        out.append({
            "file": md_file.name, "name": fm.get("name", md_file.stem), "schedule": fm.get("schedule", ""),
            "runner": fm.get("runner", ""), "role": fm.get("role", ""),
            "enabled": fm.get("enabled", "false").lower() == "true",
            "connectors": fm.get("connectors", "false").lower() == "true", "body": body,
            "gate": fm.get("gate", ""), "workdir": fm.get("workdir", ""),
        })
    return out


def execute(cron, runs):
    name = cron["name"]
    role = cron["role"]
    print(f"[dispatch] running: {name} (role={role})")
    attempt_time = now_iso()
    if not role:
        row = {"started": attempt_time, "finished": attempt_time, "duration_s": 0, "exit": 2, "provider": "",
               "model": "", "result": "fail", "summary": "cron file has no role: frontmatter"}
    else:
        row = run_cron(name, cron["body"], role, cron["connectors"], cron["gate"], cron["workdir"])
    runs[name] = attempt_time
    with open(RUNS_FILE, "w") as f:
        json.dump(runs, f, indent=2)
    record_run(name, role, row)
    print(f"[dispatch] done: {name} result={row['result']} exit={row['exit']} ({row['duration_s']}s) {row['summary'][:120]}")
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", metavar="NAME", help="run one job now regardless of schedule or enabled")
    ap.add_argument("--list", action="store_true", help="list cron files and their settings")
    args = ap.parse_args()

    if args.list:
        for c in load_crons():
            print(f"{c['name']:<26} {'on ' if c['enabled'] else 'off'} {c['schedule']:<16} "
                  f"runner={c['runner'] or '-':<6} role={c['role'] or '-':<7}{' connectors' if c['connectors'] else ''}{' gate' if c['gate'] else ''}")
        return

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = acquire_lock()  # exits 0 if already running

    now = datetime.now(timezone.utc)
    now_str = now.isoformat()

    try:
        runs = json.load(open(RUNS_FILE))
    except (OSError, ValueError):
        runs = {}

    if args.run:
        match = [c for c in load_crons() if c["name"] == args.run]
        if not match:
            sys.exit(f"no cron named {args.run}")
        row = execute(match[0], runs)
        sys.exit(0 if row["result"] in ("ok", "noop") else 1)

    for cron in load_crons():
        name = cron["name"]
        if not cron["enabled"] or cron["runner"] not in RUNNERS or not cron["schedule"]:
            continue

        if name not in runs:
            # First sight: record and skip
            runs[name] = now_str
            with open(RUNS_FILE, "w") as f:
                json.dump(runs, f, indent=2)
            print(f"[dispatch] first sight: {name}, recorded, not running")
            continue

        if not is_due(cron["schedule"], runs[name], now):
            continue
        execute(cron, runs)

    lock.close()


if __name__ == "__main__":
    main()
