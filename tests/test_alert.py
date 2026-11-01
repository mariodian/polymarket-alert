"""End-to-end tests for alert.py against a local mock of the Gamma API.

Run: python3 tests/test_alert.py
No network, no signal-cli, no state outside a temp dir.
"""

import http.server
import json
import os
import pathlib
import socketserver
import subprocess
import sys
import tempfile
import threading

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "alert.py"
WORK = pathlib.Path(tempfile.mkdtemp(prefix="polymarket-alert-test-"))

STATE = WORK / "state.json"
SENT = WORK / "sent.log"
FAKEBIN = WORK / "fakebin"
MOCK = WORK / "mock.json"
CFG_MULTI = WORK / "config.toml"
CFG_ONE = WORK / "config-one.toml"

FAKEBIN.mkdir()
_fake_signal = FAKEBIN / "signal-cli"
_fake_signal.write_text(f'#!/bin/bash\nprintf "%s\\n---\\n" "$5" >> {SENT}\nexit 0\n')
_fake_signal.chmod(0o755)
MOCK.write_text("{}")

CFG_MULTI.write_text(
    'signal_sender = "+10000000000"\n'
    'signal_recipient = "+10000000001"\n'
    "[markets]\n"
    "will-china-invade-taiwan-before-2027 = [20, 50]\n"
    "will-the-us-invade-iran-before-2027 = [40, 70]\n"
)
CFG_ONE.write_text(
    'signal_sender = "+10000000000"\n'
    'signal_recipient = "+10000000001"\n'
    "[markets]\n"
    "will-the-us-invade-iran-before-2027 = [40, 70]\n"
)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = MOCK.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


SERVER = socketserver.TCPServer(("127.0.0.1", 0), Handler)
PORT = SERVER.server_address[1]
threading.Thread(target=SERVER.serve_forever, daemon=True).start()

ENV = dict(os.environ)
# Isolate from any real ~/.config/polymarket-alert/config.toml on this machine,
# so "no arguments" means "no config anywhere".
FAKE_HOME = WORK / "home"
FAKE_HOME.mkdir()
ENV["HOME"] = str(FAKE_HOME)
ENV["XDG_CONFIG_HOME"] = str(FAKE_HOME / ".config")
ENV["XDG_CACHE_HOME"] = str(FAKE_HOME / ".cache")
ENV["POLYMARKET_API_ENDPOINT"] = f"http://127.0.0.1:{PORT}"
ENV["PATH"] = f"{FAKEBIN}:{ENV['PATH']}"
for stale in ("CONFIG_FILE", "STATE_FILE", "SIGNAL_SENDER", "SIGNAL_RECIPIENT"):
    ENV.pop(stale, None)

FAILURES: list[str] = []


def set_market(pct):
    total = pct / 100.0
    MOCK.write_text(
        json.dumps(
            {
                "question": "Will the U.S. invade Iran before 2027?",
                "slug": "will-the-us-invade-iran-before-2027",
                "bestBid": round(total - 0.005, 4),
                "bestAsk": round(total + 0.005, 4),
                "lastTradePrice": total,
                "closed": False,
                "active": True,
            }
        )
    )


def run(args, env=None):
    if SENT.exists():
        SENT.write_text("")
    result = subprocess.run(
        [sys.executable, str(SCRIPT)] + args, capture_output=True, text=True, env=env or ENV
    )
    sent = SENT.read_text() if SENT.exists() else ""
    if SENT.exists():
        SENT.write_text("")
    return result, sent


def check(label, condition, detail=""):
    print(f"{'PASS' if condition else 'FAIL'}  {label}{'' if condition else '  <- ' + str(detail).strip()}")
    if not condition:
        FAILURES.append(label)


def fired_of(slug, path=STATE):
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return data.get("slugs", {}).get(slug, {}).get("fired", [])


print("--- threshold crossing / hysteresis ---")
if STATE.exists():
    STATE.unlink()
args = ["-c", str(CFG_ONE), "--state-file", str(STATE)]
for label, price, expected in [
    ("30% -> nothing", 30, 0),
    ("42% -> fires 40", 42, 1),
    ("45% -> already fired, quiet", 45, 0),
    ("38.5% -> inside hysteresis band, quiet", 38.5, 0),
    ("37% -> falls out of the band, re-arms 40", 37, 0),
    ("41% -> fires 40 again", 41, 1),
    ("72% -> fires 70", 72, 1),
    ("71.5% -> inside hysteresis band, quiet", 71.5, 0),
]:
    set_market(price)
    result, sent = run(args)
    alerts = sent.count("Crossed")
    check(f"{label} (alerts={alerts}, fired={fired_of('will-the-us-invade-iran-before-2027')})",
          result.returncode == 0 and alerts == expected, result.stderr)
    if expected and "Crossed" in sent:
        print("      " + sent.strip().split("\n---")[0].replace("\n", "\n      "))

print("\n--- state format ---")
state = json.loads(STATE.read_text())
check("state entry keeps the legacy shape",
      set(state["slugs"]["will-the-us-invade-iran-before-2027"]) == {"fired", "last", "thresholds", "ts"})

legacy = WORK / "legacy-state.json"
legacy.write_text(json.dumps({"slugs": {"will-the-us-invade-iran-before-2027": {"fired": ["40", "70"], "last": 72}}}))
set_market(80)
result, sent = run(["-c", str(CFG_ONE), "--state-file", str(legacy)])
check("string thresholds in an existing state file are understood (no re-alert)", sent.count("Crossed") == 0, sent)

print("\n--- error handling ---")
MOCK.write_text("{}")
result, _ = run(args)
check("unknown market -> exit 1", result.returncode == 1)
check("unknown market -> clear stderr", "no market found" in result.stderr, result.stderr)

bad = WORK / "bad.toml"
bad.write_text('signal_sender = "+1"\nnot_a_key = 1\n[markets]\nx = [20]\n')
result, _ = run(["-c", str(bad), "--dry-run"])
check("unknown top-level key rejected", result.returncode == 1 and "unknown key" in result.stderr, result.stderr)

bad.write_text("[markets]\nx = [140]\n")
result, _ = run(["-c", str(bad), "--dry-run"])
check("threshold outside 0-100 rejected", result.returncode == 1 and "outside 0-100" in result.stderr, result.stderr)

bad.write_text("[markets\nx = [20]\n")
result, _ = run(["-c", str(bad), "--dry-run"])
check("broken TOML rejected with the parser message", result.returncode == 1 and "invalid TOML" in result.stderr, result.stderr)

result, _ = run(["-c", str(WORK / "nope.toml")])
check("missing config file rejected", result.returncode == 1 and "not found" in result.stderr, result.stderr)

result, _ = run([])
check("no markets at all -> exit 1", result.returncode == 1 and "no markets" in result.stderr, result.stderr)

if STATE.exists():
    STATE.unlink()
set_market(85)
no_signal = dict(ENV)
no_signal["PATH"] = "/usr/bin:/bin"  # signal-cli not reachable
result, _ = run(["-c", str(CFG_ONE), "--state-file", str(STATE)], env=no_signal)
check("failed send -> exit 1 and the threshold is NOT marked fired",
      result.returncode == 1 and 70 not in fired_of("will-the-us-invade-iran-before-2027", STATE),
      f"rc={result.returncode} fired={fired_of('will-the-us-invade-iran-before-2027', STATE)}")

print("\n--- config and CLI precedence ---")
result, _ = run(["-c", str(CFG_MULTI), "--list"])
check("--list reflects the config", "will-the-us-invade-iran-before-2027\t40,70\tmid" in result.stdout, result.stdout)

result, _ = run(["-c", str(CFG_MULTI), "--list", "-w", "some-other-market:10,90"])
check("a CLI market is added to the config markets",
      "some-other-market\t10,90" in result.stdout and "will-china-invade-taiwan" in result.stdout, result.stdout)

result, _ = run(["-c", str(CFG_MULTI), "--list", "-w", "will-the-us-invade-iran-before-2027:5"])
check("a CLI market overrides the same slug from the config",
      "will-the-us-invade-iran-before-2027\t5\t" in result.stdout and "40,70" not in result.stdout, result.stdout)

override = WORK / "override.toml"
override.write_text(
    'hysteresis = 2\nprice_source = "mid"\n[markets]\n'
    'a-market = { thresholds = [30, 60], price_source = "ask", hysteresis = 5 }\n'
    "b-market = [10]\n"
)
result, _ = run(["-c", str(override), "--list"])
check("per-market overrides honoured",
      "a-market\t30,60\task\thysteresis=5" in result.stdout
      and "b-market\t10\tmid\thysteresis=2" in result.stdout, result.stdout)

result, _ = run(["-c", str(override), "--list", "--price-source", "last"])
check("CLI --price-source beats the config default",
      "b-market\t10\tlast" in result.stdout and "a-market\t30,60\task" in result.stdout, result.stdout)

SERVER.shutdown()
print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) failed")
    sys.exit(1)
print("all checks passed")
