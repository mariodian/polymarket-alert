# polymarket-alert

Watch Polymarket yes/no markets and get a Signal message when the implied
probability crosses the thresholds you care about. Any number of markets, any
number of thresholds per market.

## Features

- Monitor any market by slug — no code changes to add a new prediction.
- Per-market threshold lists: `-t 40,70` fires at 40% and again at 70%.
- Fires **once per upward crossing**, then goes quiet until the price drops
  back below the threshold (minus a hysteresis margin) and crosses again.
  Without this, a cron job above threshold would nag you every single run.
- Graceful handling of illiquid markets (missing bid/ask), and a non-zero exit
  code on API/parse failure so a broken check never looks like "no news".
- Emoji severity scale identical to the original script (🟢🟡🟠🔴⚫).

## Requirements

- Bash, `curl`, `jq`, `awk`
- `signal-cli`, configured with a sender number

## Usage

```bash
./alert.sh -w <slug>:<thresholds> [-w <slug>:<thresholds> ...] \
           -ss <signal-sender> -sr <signal-recipient>
```

### Examples

One market, notify at 40% and 70%:

```bash
./alert.sh -w will-the-us-invade-iran-before-2027:40,70 \
           -ss +1234567890 -sr +0987654321
```

Several markets with different thresholds:

```bash
./alert.sh \
  -s will-china-invade-taiwan-before-2027 -t 20,50 \
  -s will-the-us-invade-iran-before-2027  -t 40,70 \
  -ss +1234567890 -sr +0987654321
```

The `-s/-t` form and the `-w` form are equivalent and can be mixed. A `-t`
applies to every slug declared before it that has no explicit threshold yet,
and to every slug declared after it until the next `-t`.

### Finding the slug

Take it from the URL: `polymarket.com/market/will-the-us-invade-iran-before-2027`
→ `will-the-us-invade-iran-before-2027`. For multi-outcome events, the URL
points at the event (`/event/<event-slug>`); use the slug of the individual
yes/no market inside it.

### Alert format

```
Will the U.S. invade Iran before 2027?
🟠 Maybe. The chance is moderate (42.0%).
📈 Crossed 40% threshold
https://polymarket.com/market/will-the-us-invade-iran-before-2027
```

## Options

| Flag | Meaning |
|---|---|
| `-s, --slug=SLUG` | Market slug, repeatable |
| `-t, --threshold=LIST` | Comma separated percentages, e.g. `40,70` (default 60) |
| `-w, --watch=SLUG:LIST` | Compact form of `-s` + `-t`, repeatable |
| `-ss, --signal-sender` | Sender number for signal-cli |
| `-sr, --signal-recipient` | Recipient number |
| `--price-source=mid\|ask\|last` | Probability source. `mid` = `(bestBid+bestAsk)/2` (default), `ask` = what YES actually costs, `last` = last trade |
| `--hysteresis=N` | Points below a threshold the price must fall before it can fire again (default 2) |
| `--state-file=PATH` | Where fired thresholds are remembered (default `~/.cache/polymarket-alert/state.json`) |
| `--no-state` | Ignore state and alert whenever the price is over a threshold |
| `--dry-run` | Print the alert instead of sending it |
| `-h, --help` | Usage |

The same values can be set through the environment: `THRESHOLD`, `HYSTERESIS`,
`PRICE_SOURCE`, `STATE_FILE`, `SIGNAL_SENDER`, `SIGNAL_RECIPIENT`,
`POLYMARKET_API_ENDPOINT`.

## Cron

```
12 */6 * * * $HOME/source/Scripts/polymarket-alert/alert.sh \
  -w will-china-invade-taiwan-before-2027:20,50 \
  -w will-the-us-invade-iran-before-2027:40,70 \
  -ss +1234567890 -sr +0987654321 >/dev/null 2>&1
```

Adding a market = adding one `-w slug:t1,t2` argument. Nothing else changes.

### Notes on state

`~/.cache/polymarket-alert/state.json` records, per slug, which thresholds are
currently "fired" and the price at the last run. If the send fails, the
threshold is not marked fired, so it retries on the next run. Deleting the file
resets everything and any market currently over its thresholds will alert once
on the next run.

## Exit codes

- `0` — checked successfully (whether or not anything fired)
- `1` — bad arguments, no API response, unparseable market, or a failed send

## License

This project is unlicensed and free to use as you see fit.
