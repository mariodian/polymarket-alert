#!/usr/bin/env python3
"""polymarket-alert — watch Polymarket yes/no markets and alert via Signal when
the implied probability crosses the thresholds you care about.

Python 3.11+ stdlib only: no jq, curl or bc.

Typical cron use — everything lives in a TOML file:

    alert.py -c ~/.config/polymarket-alert/config.toml

Ad-hoc single check:

    alert.py -w will-the-us-invade-iran-before-2027:40,70 \\
             -ss +1234567890 -sr +0987654321 --dry-run

Exit codes: 0 = checked fine (fired or not), 1 = bad config/args or a
failed fetch/send, so a broken check never looks like "no news".
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

VERSION = "2.0.0"

DEFAULT_API = "https://gamma-api.polymarket.com"
DEFAULT_THRESHOLDS = "60"
DEFAULT_HYSTERESIS = 2.0
DEFAULT_PRICE_SOURCE = "mid"
DEFAULT_TIMEOUT = 20.0
PRICE_SOURCES = ("mid", "ask", "last")

# (upper bound, emoji, message). The first band whose bound the price is under wins.
TIERS = (
    (20.0, "🟢", "No. The chance is very low ({p}%)."),
    (40.0, "🟡", "Unlikely. The chance is somewhat low ({p}%)."),
    (60.0, "🟠", "Maybe. The chance is moderate ({p}%)."),
    (80.0, "🔴", "Likely. The chance is somewhat high ({p}%)."),
    (float("inf"), "⚫", "Yes. The chance is very high ({p}%)."),
)

CONFIG_KEYS = {
    "signal_sender",
    "signal_recipient",
    "signal_cli",
    "hysteresis",
    "price_source",
    "state_file",
    "api_endpoint",
    "timeout",
    "thresholds",
    "markets",
}


class ConfigError(Exception):
    """Bad arguments or a bad config file — always fatal."""


class MarketError(Exception):
    """A single market could not be checked — fatal for that market only."""


# --------------------------------------------------------------------------- #
# settings / config
# --------------------------------------------------------------------------- #


@dataclass
class Watch:
    slug: str
    thresholds: list[float]
    price_source: str
    hysteresis: float
    label: str = ""

    @property
    def name(self) -> str:
        return self.label or self.slug


@dataclass
class Settings:
    signal_sender: str = ""
    signal_recipient: str = ""
    signal_cli: str = "signal-cli"
    hysteresis: float = DEFAULT_HYSTERESIS
    price_source: str = DEFAULT_PRICE_SOURCE
    state_file: Path = field(
        default_factory=lambda: Path(
            os.environ.get(
                "XDG_CACHE_HOME", Path.home() / ".cache"
            )
        )
        / "polymarket-alert"
        / "state.json"
    )
    api_endpoint: str = DEFAULT_API
    timeout: float = DEFAULT_TIMEOUT
    markets: list[Watch] = field(default_factory=list)


def default_config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "polymarket-alert" / "config.toml"


def parse_thresholds(raw: Any, where: str) -> list[float]:
    """Accept [40, 70], "40,70" or 40 -> sorted unique list of floats in 0..100."""
    values: list[Any]
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        values = [raw]
    elif isinstance(raw, str):
        values = [part for part in raw.replace(" ", "").split(",") if part]
    elif isinstance(raw, (list, tuple)):
        values = list(raw)
    else:
        raise ConfigError(f"{where}: thresholds must be a number, a list or '40,70'")

    out: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{where}: '{value}' is not a number") from None
        if not 0.0 <= number <= 100.0:
            raise ConfigError(f"{where}: threshold {number} outside 0-100")
        if number not in out:
            out.append(number)
    if not out:
        raise ConfigError(f"{where}: no thresholds given")
    return sorted(out)


def parse_watch_spec(spec: str) -> tuple[str, str]:
    """'slug:40,70' -> ('slug', '40,70'); 'slug' -> ('slug', '')."""
    if ":" in spec:
        slug, _, raw = spec.partition(":")
        return slug.strip(), raw.strip()
    return spec.strip(), ""


def load_config(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML — {exc}") from None

    unknown = set(data) - CONFIG_KEYS
    if unknown:
        raise ConfigError(
            f"{path}: unknown key(s) {', '.join(sorted(unknown))}. "
            f"Valid keys: {', '.join(sorted(CONFIG_KEYS))}"
        )
    return data


def watches_from_config(cfg: dict[str, Any], where: str) -> tuple[list[Watch], dict[str, Any]]:
    """Build the market list from a config dict; returns (watches, loose settings)."""
    settings: dict[str, Any] = {
        key: cfg[key] for key in cfg if key not in ("markets", "thresholds")
    }

    markets = cfg.get("markets", {})
    if not isinstance(markets, dict):
        raise ConfigError(f"{where}: [markets] must be a table")

    default_thresholds = cfg.get("thresholds", DEFAULT_THRESHOLDS)
    watches: list[Watch] = []
    for slug, spec in markets.items():
        at = f"{where}: market '{slug}'"
        overrides: dict[str, Any] = {}
        if isinstance(spec, dict):
            unknown = set(spec) - {"thresholds", "price_source", "hysteresis", "label"}
            if unknown:
                raise ConfigError(
                    f"{at}: unknown key(s) {', '.join(sorted(unknown))}"
                )
            thresholds_raw = spec.get("thresholds", default_thresholds)
            overrides = {k: v for k, v in spec.items() if k != "thresholds"}
        else:
            thresholds_raw = spec if spec not in ("", []) else default_thresholds

        try:
            market_hysteresis = float(overrides.pop("hysteresis", -1.0))
        except (TypeError, ValueError):
            raise ConfigError(f"{at}: hysteresis must be a number") from None

        watches.append(
            Watch(
                slug=slug,
                thresholds=parse_thresholds(thresholds_raw, at),
                price_source=str(overrides.pop("price_source", "")).lower(),
                hysteresis=market_hysteresis,
                label=str(overrides.pop("label", "")),
            )
        )
    return watches, settings


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def fetch_market(slug: str, settings: Settings) -> dict[str, Any]:
    url = f"{settings.api_endpoint.rstrip('/')}/markets/slug/{quote(slug)}"
    request = urllib.request.Request(
        url, headers={"User-Agent": f"polymarket-alert/{VERSION}", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise MarketError(f"no market found for slug '{slug}'") from None
        raise MarketError(f"HTTP {exc.code} from Polymarket for '{slug}'") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MarketError(f"no response from Polymarket for '{slug}': {exc}") from None

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise MarketError(f"unparseable response for '{slug}': {body[:160]!r}") from None
    if not isinstance(data, dict) or not data.get("question"):
        raise MarketError(f"no market found for slug '{slug}'")
    return data


def as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def implied_percent(market: dict[str, Any], source: str) -> float:
    """Probability in percent (0-100) from the requested price source."""
    bid = as_float(market.get("bestBid"))
    ask = as_float(market.get("bestAsk"))
    last = as_float(market.get("lastTradePrice"))

    mid = None
    if bid is not None and ask is not None:
        mid = (bid + ask) / 2
    elif ask is not None:
        mid = ask
    elif bid is not None:
        mid = bid

    if source == "ask":
        price = ask if ask is not None else (last if last is not None else mid)
    elif source == "last":
        price = last if last is not None else mid
    else:
        price = mid

    if price is None:
        raise MarketError(
            f"no usable price (bid={market.get('bestBid')} "
            f"ask={market.get('bestAsk')} last={market.get('lastTradePrice')})"
        )
    return max(0.0, min(100.0, price * 100))


def fmt_pct(value: float) -> str:
    """One decimal, matching what the shell version printed (round to 4dp first)."""
    return f"{round(value, 4):.1f}"


def tier_for(percent: float) -> tuple[str, str]:
    for bound, emoji, template in TIERS:
        if percent < bound:
            return emoji, template.format(p=fmt_pct(percent))
    return TIERS[-1][1], TIERS[-1][2].format(p=fmt_pct(percent))


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #


def load_state(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and isinstance(data.get("slugs", {}), dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"slugs": {}}


def fired_thresholds(state: dict[str, Any], slug: str) -> list[float]:
    entry = state.get("slugs", {}).get(slug, {})
    out: list[float] = []
    for value in entry.get("fired", []) or []:
        number = as_float(value)
        if number is not None:
            out.append(number)
    return out


def save_state(path: Path, state: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        print(f"Warning: could not write state file {path}: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# notify
# --------------------------------------------------------------------------- #


def resolve_signal_cli(name: str) -> str:
    if os.sep in name:
        return name
    found = shutil.which(name)
    if not found:
        raise ConfigError(
            f"'{name}' not found on PATH ({os.environ.get('PATH', '')}). "
            "Set signal_cli in the config or fix PATH in cron."
        )
    return found


def send_signal(message: str, settings: Settings) -> None:
    binary = resolve_signal_cli(settings.signal_cli)
    command = [
        binary,
        "-a",
        settings.signal_sender,
        "send",
        "-m",
        message,
        settings.signal_recipient,
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise MarketError(f"signal-cli failed: {detail[-1] if detail else 'unknown error'}")


# --------------------------------------------------------------------------- #
# per-market check
# --------------------------------------------------------------------------- #


def check_watch(
    watch: Watch, settings: Settings, state: dict[str, Any], *, dry_run: bool, no_state: bool
) -> tuple[bool, str]:
    """Returns (ok, note). ok=False means this market's check failed."""
    try:
        market = fetch_market(watch.slug, settings)
        percent = implied_percent(market, watch.price_source)
    except MarketError as exc:
        print(f"Error: {watch.slug}: {exc}", file=sys.stderr)
        return False, ""

    question = str(market["question"])
    emoji, tier = tier_for(percent)
    previous = as_float(state.get("slugs", {}).get(watch.slug, {}).get("last"))
    fired = [] if no_state else fired_thresholds(state, watch.slug)

    print(question)
    print(f"{emoji} {tier}")
    if market.get("closed"):
        print("   (market is closed)")
    print(
        f"   slug={watch.slug} price={fmt_pct(percent)}% source={watch.price_source} "
        f"thresholds={','.join(f'{t:g}' for t in watch.thresholds)}"
    )

    new_hits: list[float] = []
    kept: list[float] = []
    for threshold in watch.thresholds:
        already = threshold in fired
        if percent >= threshold:
            kept.append(threshold)
            if not already:
                new_hits.append(threshold)
        elif already and percent > threshold - watch.hysteresis:
            kept.append(threshold)  # still inside the hysteresis band: stay armed-down
        # else: dropped far enough below -> re-arm (drop from the list)

    if not new_hits:
        if not no_state:
            state.setdefault("slugs", {})[watch.slug] = {
                "fired": kept,
                "last": round(percent, 2),
                "thresholds": watch.thresholds,
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        return True, "quiet"

    lines = [question, f"{emoji} {tier}"]
    lines += [f"📈 Crossed {t:g}% threshold" for t in new_hits]
    lines.append(f"https://polymarket.com/market/{watch.slug}")
    message = "\n".join(lines)

    move = ""
    if previous is not None:
        move = f" (was {fmt_pct(previous)}%)"

    if dry_run:
        print("--- DRY RUN, would send ---")
        print(message)
        print("---------------------------")
    else:
        try:
            if not settings.signal_sender or not settings.signal_recipient:
                raise MarketError(
                    "no signal sender/recipient configured (signal_sender, "
                    "signal_recipient in the config, or -ss/-sr)"
                )
            send_signal(message, settings)
        except (MarketError, ConfigError) as exc:
            print(f"Error: {watch.slug}: {exc}", file=sys.stderr)
            # Do not mark as fired when the send failed -> retried next run.
            return False, ""
        crossed = ",".join(f"{t:g}" for t in new_hits)
        print(f"   alerted: crossed {crossed}%{move}")

    if not no_state:
        state.setdefault("slugs", {})[watch.slug] = {
            "fired": kept,
            "last": round(percent, 2),
            "thresholds": watch.thresholds,
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    return True, "alerted"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alert.py",
        description="Watch Polymarket yes/no markets and alert via Signal when the "
        "implied probability crosses given thresholds.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  alert.py -c ~/.config/polymarket-alert/config.toml
  alert.py -w will-the-us-invade-iran-before-2027:40,70 -ss +123 -sr +456 --dry-run
  alert.py -s will-china-invade-taiwan-before-2027 -t 20,50 -ss +123 -sr +456
""",
    )
    parser.add_argument(
        "-c",
        "--config",
        metavar="FILE",
        help=f"TOML config (default: {default_config_path()} if it exists)",
    )
    parser.add_argument(
        "-w",
        "--watch",
        action="append",
        default=[],
        metavar="SLUG:THRESHOLDS",
        help="market and thresholds, repeatable, e.g. slug:40,70",
    )
    parser.add_argument(
        "-s",
        "--slug",
        action="append",
        default=[],
        metavar="SLUG",
        help="market slug, repeatable; thresholds come from -t",
    )
    parser.add_argument(
        "-t",
        "--threshold",
        metavar="LIST",
        help=f"comma separated percentages for -s/-w without inline thresholds "
        f"(default {DEFAULT_THRESHOLDS})",
    )
    parser.add_argument("--signal-sender", "-ss", metavar="NUMBER", help="sender (signal-cli account)")
    parser.add_argument("--signal-recipient", "-sr", metavar="NUMBER", help="recipient")
    parser.add_argument("--signal-cli", metavar="CMD", help="signal-cli binary or path (default signal-cli)")
    parser.add_argument(
        "--price-source",
        choices=PRICE_SOURCES,
        help=f"mid = (bid+ask)/2 (default), ask = what YES costs, last = last trade",
    )
    parser.add_argument(
        "--hysteresis",
        type=float,
        metavar="N",
        help=f"points the price must fall back below a threshold before it can fire "
        f"again (default {DEFAULT_HYSTERESIS:g})",
    )
    parser.add_argument("--state-file", metavar="PATH", help="where fired thresholds are remembered")
    parser.add_argument("--no-state", action="store_true", help="ignore state, alert whenever over a threshold")
    parser.add_argument("--dry-run", action="store_true", help="print the alert instead of sending it")
    parser.add_argument("--list", action="store_true", help="print the resolved watch list and exit")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def build_settings(args: argparse.Namespace) -> Settings:
    settings = Settings(
        signal_sender=os.environ.get("SIGNAL_SENDER", ""),
        signal_recipient=os.environ.get("SIGNAL_RECIPIENT", ""),
        signal_cli=os.environ.get("SIGNAL_CLI", "signal-cli"),
        hysteresis=float(os.environ.get("HYSTERESIS", DEFAULT_HYSTERESIS)),
        price_source=os.environ.get("PRICE_SOURCE", DEFAULT_PRICE_SOURCE),
        api_endpoint=os.environ.get("POLYMARKET_API_ENDPOINT", DEFAULT_API),
    )
    if os.environ.get("STATE_FILE"):
        settings.state_file = Path(os.environ["STATE_FILE"])

    # --- config file -------------------------------------------------------
    config_path: Path | None = None
    if args.config:
        config_path = Path(args.config).expanduser()
    else:
        env_path = os.environ.get("CONFIG_FILE")
        candidate = Path(env_path).expanduser() if env_path else default_config_path()
        if candidate.exists():
            config_path = candidate

    config_watches: list[Watch] = []
    if config_path is not None:
        cfg = load_config(config_path)
        where = str(config_path)
        config_watches, loose = watches_from_config(cfg, where)

        for key, value in loose.items():
            if key == "signal_sender":
                settings.signal_sender = str(value)
            elif key == "signal_recipient":
                settings.signal_recipient = str(value)
            elif key == "signal_cli":
                settings.signal_cli = str(value)
            elif key == "hysteresis":
                settings.hysteresis = float(value)
            elif key == "price_source":
                source = str(value).lower()
                if source not in PRICE_SOURCES:
                    raise ConfigError(f"{where}: price_source must be one of {PRICE_SOURCES}")
                settings.price_source = source
            elif key == "state_file":
                settings.state_file = Path(str(value)).expanduser()
            elif key == "api_endpoint":
                settings.api_endpoint = str(value)
            elif key == "timeout":
                settings.timeout = float(value)

    # --- command line wins over config, config wins over env ---------------
    if args.signal_sender:
        settings.signal_sender = args.signal_sender
    if args.signal_recipient:
        settings.signal_recipient = args.signal_recipient
    if args.signal_cli:
        settings.signal_cli = args.signal_cli
    if args.price_source:
        settings.price_source = args.price_source
    if args.hysteresis is not None:
        settings.hysteresis = args.hysteresis
    if args.state_file:
        settings.state_file = Path(args.state_file).expanduser()

    if settings.price_source not in PRICE_SOURCES:
        raise ConfigError(f"price_source must be one of {PRICE_SOURCES}")

    # --- assemble the watch list ------------------------------------------
    cli_watches: list[Watch] = []
    cli_thresholds = parse_thresholds(args.threshold or DEFAULT_THRESHOLDS, "--threshold")

    for spec in args.watch:
        slug, raw = parse_watch_spec(spec)
        if not slug:
            raise ConfigError(f"-w '{spec}': empty slug")
        cli_watches.append(
            Watch(
                slug=slug,
                thresholds=parse_thresholds(raw or cli_thresholds, f"-w {slug}"),
                price_source=settings.price_source,
                hysteresis=settings.hysteresis,
            )
        )
    for slug in args.slug:
        slug = slug.strip()
        if not slug:
            raise ConfigError("-s: empty slug")
        cli_watches.append(
            Watch(
                slug=slug,
                thresholds=cli_thresholds,
                price_source=settings.price_source,
                hysteresis=settings.hysteresis,
            )
        )

    merged: dict[str, Watch] = {}
    for watch in config_watches:  # config markets keep their own overrides
        watch.price_source = watch.price_source or settings.price_source
        watch.hysteresis = watch.hysteresis if watch.hysteresis >= 0 else settings.hysteresis
        merged[watch.slug] = watch
    for watch in cli_watches:  # CLI replaces the same slug from the config
        merged[watch.slug] = watch

    if not merged:
        raise ConfigError(
            "no markets to watch — pass -c CONFIG, or -w/-s on the command line"
        )
    settings.markets = list(merged.values())
    return settings


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = build_settings(args)
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.list:
        for watch in settings.markets:
            thresholds = ",".join(f"{t:g}" for t in watch.thresholds)
            print(f"{watch.slug}\t{thresholds}\t{watch.price_source}\thysteresis={watch.hysteresis:g}")
        return 0

    state = {"slugs": {}} if args.no_state else load_state(settings.state_file)
    rc = 0
    for watch in settings.markets:
        ok, _ = check_watch(
            watch, settings, state, dry_run=args.dry_run, no_state=args.no_state
        )
        if not ok:
            rc = 1

    if not args.no_state:
        save_state(settings.state_file, state)
    return rc


if __name__ == "__main__":
    sys.exit(main())
