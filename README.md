# polymarket-alert

Watch Polymarket yes/no markets and get a Signal message when their implied
probability crosses thresholds you set. Any number of markets, any number of
thresholds per market, all defined in one TOML file.

Watch twenty predictions without the command line growing a single flag.

## Requirements

- Python 3.11+ (stdlib only — `tomllib`, `urllib`, `subprocess`; no `jq`, `curl` or `bc`)
- `signal-cli` for delivery

## Install

```bash
git clone git@github.com:mariodian/polymarket-alert.git
cd polymarket-alert
mkdir -p ~/.config/polymarket-alert
cp config.toml.example ~/.config/polymarket-alert/config.toml
$EDITOR ~/.config/polymarket-alert/config.toml
```

`alert.py` looks for `~/.config/polymarket-alert/config.toml` (or
`$XDG_CONFIG_HOME/...`) when `-c` is omitted, so from then on the whole command
is just `alert.py`.

## Config

```toml
# --- Signal delivery ---------------------------------------------------
signal_sender    = "+421..."
signal_recipient = "+886..."
# signal_cli     = "signal-cli"      # name on PATH, or an absolute path

# --- defaults for every market below -----------------------------------
hysteresis   = 2                     # points below a threshold before it re-arms
price_source = "mid"                 # mid | ask | last
# state_file = "~/.cache/polymarket-alert/state.json"
# thresholds = 60                    # fallback when a market lists none

[markets]
will-china-invade-taiwan-before-2027 = [20, 50]
will-the-us-invade-iran-before-2027  = [40, 70]
```

Adding a prediction is one line: `slug = [thresholds]`.

Per-market overrides, when one market needs a different source or margin:

```toml
[markets]
will-fed-cut-rates-in-december = { thresholds = [25, 75], price_source = "ask", hysteresis = 3 }
```

Unknown keys are rejected rather than ignored — a typo in `signal_reciepent` is
otherwise a monitor that silently never alerts.

### Finding the slug

It is the last segment of the market URL:
`polymarket.com/market/will-the-us-invade-iran-before-2027` →
`will-the-us-invade-iran-before-2027`. For a multi-outcome event
(`/event/<event-slug>`) use the slug of the individual yes/no market inside it.

## Running

```bash
alert.py                      # uses the default config path
alert.py -c /path/config.toml # explicit config
alert.py --list               # show what would be watched, then exit
alert.py -w will-the-us-invade-iran-before-2027:40,70 --dry-run   # ad-hoc, no config
```

CLI options override the config, which overrides environment variables, which
override the built-in defaults. `--dry-run` prints the alert instead of sending
it; `--list` resolves the config and prints the watch list.

## Behaviour

**Each threshold fires once per upward crossing.** The state file remembers
which thresholds have fired, so a job running four times a day sends one
message per event, not one per run. A threshold re-arms only after the price
falls `hysteresis` points back below it, which stops flapping around 40.0%.

**A failed send is retried.** The threshold is only marked fired once
`signal-cli` reports success.

**Exit code 1 on any fetch, parse, config or send failure**, so a broken check
never looks like "no news". Per-market failures don't stop the other markets.

**Price source.** `mid` = `(bestBid + bestAsk) / 2` (default, the honest
probability), `ask` = what YES costs, `last` = last trade.

Alert format:

```
Will the U.S. invade Iran before 2027?
🟠 Maybe. The chance is moderate (42.0%).
📈 Crossed 40% threshold
https://polymarket.com/market/will-the-us-invade-iran-before-2027
```

## Cron

```
12 */6 * * * PATH=/usr/local/bin:/usr/bin:/bin $HOME/source/Scripts/polymarket-alert/alert.py \
  -c $HOME/.config/polymarket-alert/config.toml > $HOME/logs/polymarket-alert.log 2>&1
```

`PATH` is set explicitly because cron's default (`/usr/bin:/bin`) usually does
not include `/usr/local/bin`, where `signal-cli` tends to live. The log
truncates each run, so the last run's output is always the current one.

## Tests

```bash
python3 tests/test_alert.py
```

Runs against a local mock of the Gamma API — no network, no `signal-cli`, no
state outside a temp dir. Covers crossings, hysteresis re-arming, state-format
compatibility, config/CLI precedence and the error paths.

## Historically

Was a bash script taking `-s slug -t threshold`. Still accepts that shape for a
quick single check, but the config file is the intended interface.

## License

Unlicensed, free to use as you see fit.
