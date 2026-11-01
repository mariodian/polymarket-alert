#!/bin/bash
#
# polymarket-alert — watch Polymarket yes/no markets and ping Signal when the
# implied probability crosses the thresholds you care about.
#
# One slug, one threshold:
#   ./alert.sh -s will-the-us-invade-iran-before-2027 -t 40 -ss +1... -sr +1...
#
# Many markets, different thresholds (repeat -s/-t; each -t applies to the
# slugs declared before it, and to every slug declared after it until the next -t):
#   ./alert.sh -s will-china-invade-taiwan-before-2027 -t 20,50 \
#              -s will-the-us-invade-iran-before-2027 -t 40,70 \
#              -ss +1... -sr +1...
#
# Or the compact form, one flag per market:
#   ./alert.sh -w will-the-us-invade-iran-before-2027:40,70 -ss +1... -sr +1...

set -u

SCRIPT_NAME="./$(basename "$0")"
API_ENDPOINT="${POLYMARKET_API_ENDPOINT:-https://gamma-api.polymarket.com}"

THRESHOLDS_DEFAULT="${THRESHOLD:-60}"
HYSTERESIS="${HYSTERESIS:-2}"
PRICE_SOURCE="${PRICE_SOURCE:-mid}"
STATE_FILE="${STATE_FILE:-${XDG_CACHE_HOME:-$HOME/.cache}/polymarket-alert/state.json}"

SLUGS=()
SPECS=()
INHERIT=()      # 1 = spec came from an earlier -t and may be replaced by a later one
PENDING_SPEC=""
SIGNAL_SENDER="${SIGNAL_SENDER:-}"
SIGNAL_RECIPIENT="${SIGNAL_RECIPIENT:-}"
DRY_RUN=0
NO_STATE=0

show_usage() {
  cat << EOF
Watch Polymarket yes/no markets and send Signal alerts when the implied
probability crosses given thresholds.

Usage: $SCRIPT_NAME [OPTIONS]

Markets:
  -s --slug=SLUG             Polymarket market slug (repeatable)
                             e.g. will-the-us-invade-iran-before-2027
  -t --threshold=LIST        Comma separated percentages, e.g. 40,70
                             (default: ${THRESHOLDS_DEFAULT})
                             Applies to the slugs declared before it and to any
                             slug declared after it until the next -t.
  -w --watch=SLUG:LIST       Compact form, repeatable. -w slug:40,70

Signal notification:
  -ss --signal-sender=SENDER     Sender number (needs signal-cli)
  -sr --signal-recipient=RCVR    Recipient number (needs signal-cli)

Tuning:
  --price-source=SRC         mid (default) | ask | last
                             mid  = (bestBid + bestAsk) / 2
                             ask  = bestAsk (what YES actually costs)
                             last = lastTradePrice
  --hysteresis=N             Re-arm a threshold only after the price falls
                             N points back below it (default: ${HYSTERESIS}).
                             Stops flapping around 40.0%.
  --state-file=PATH          Where fired thresholds are remembered
                             (default: \$XDG_CACHE_HOME/polymarket-alert/state.json)
  --no-state                 Ignore state: alert whenever price is over threshold
  --dry-run                  Print the alert, do not send it
  -h --help                  This help

Environment: THRESHOLD, HYSTERESIS, PRICE_SOURCE, STATE_FILE,
             SIGNAL_SENDER, SIGNAL_RECIPIENT, POLYMARKET_API_ENDPOINT

Behaviour:
  * Each threshold fires once per upward crossing, then goes quiet until the
    price drops back below it by the hysteresis margin. No 4x/day nagging.
  * Exit code 1 on API/parse failure, so a dead check never looks like "quiet".

Example:
  $SCRIPT_NAME -s will-the-us-invade-iran-before-2027 -t 40,70 \\
               -ss +1234567890 -sr +0987654321
EOF
  exit "${1:-1}"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "Error: '$1' is required but not installed." >&2; exit 1; }
}

# --- argument parsing -------------------------------------------------------
while [[ $# -gt 0 ]]; do
  goto_t=0; goto_w=0
  case $1 in
    --slug=*|-s=*)              SLUGS+=("${1#*=}"); SPECS+=("$PENDING_SPEC"); INHERIT+=("$([[ -n "$PENDING_SPEC" ]] && echo 1 || echo 0)"); shift ;;
    --slug|-s)                  SLUGS+=("$2"); SPECS+=("$PENDING_SPEC"); INHERIT+=("$([[ -n "$PENDING_SPEC" ]] && echo 1 || echo 0)"); shift 2 ;;
    --threshold=*|-t=*)         _t="${1#*=}"; goto_t=1; shift ;;
    --threshold|-t)             _t="$2"; goto_t=1; shift 2 ;;
    --watch=*|-w=*)             _w="${1#*=}"; goto_w=1; shift ;;
    --watch|-w)                 _w="$2"; goto_w=1; shift 2 ;;
    --signal-sender=*|-ss=*)    SIGNAL_SENDER="${1#*=}"; shift ;;
    --signal-sender|-ss)        SIGNAL_SENDER="$2"; shift 2 ;;
    --signal-recipient=*|-sr=*) SIGNAL_RECIPIENT="${1#*=}"; shift ;;
    --signal-recipient|-sr)     SIGNAL_RECIPIENT="$2"; shift 2 ;;
    --price-source=*)           PRICE_SOURCE="${1#*=}"; shift ;;
    --price-source)             PRICE_SOURCE="$2"; shift 2 ;;
    --hysteresis=*)             HYSTERESIS="${1#*=}"; shift ;;
    --hysteresis)               HYSTERESIS="$2"; shift 2 ;;
    --state-file=*)             STATE_FILE="${1#*=}"; shift ;;
    --state-file)               STATE_FILE="$2"; shift 2 ;;
    --no-state)                 NO_STATE=1; shift ;;
    --dry-run)                  DRY_RUN=1; shift ;;
    --help|-h)                  show_usage 0 ;;
    *) echo "Unknown argument: $1" >&2; echo "See $SCRIPT_NAME --help" >&2; exit 1 ;;
  esac

  # -t: binds to every slug declared before it that has no explicit threshold
  # yet, replaces inherited ones, and becomes the default for later slugs.
  if [[ $goto_t -eq 1 ]]; then
    PENDING_SPEC="$_t"
    for i in "${!SPECS[@]}"; do
      if [[ -z "${SPECS[$i]}" || "${INHERIT[$i]:-0}" == "1" ]]; then
        SPECS[$i]="$_t"
        INHERIT[$i]=0
      fi
    done
  fi

  # -w: split slug:thresholds (explicit, never overridden by -t)
  if [[ $goto_w -eq 1 ]]; then
    SLUGS+=("${_w%%:*}")
    SPECS+=("${_w#*:}")
    INHERIT+=(0)
  fi
done

if [[ ${#SLUGS[@]} -eq 0 ]]; then
  echo "Error: No market slug specified." >&2
  echo "See $SCRIPT_NAME --help" >&2
  exit 1
fi

if [[ ! "$SIGNAL_SENDER" || ! "$SIGNAL_RECIPIENT" ]]; then
  echo "Error: --signal-sender and --signal-recipient are both required." >&2
  echo "See $SCRIPT_NAME --help" >&2
  exit 1
fi

case "$PRICE_SOURCE" in
  mid|ask|last) ;;
  *) echo "Error: --price-source must be mid, ask or last." >&2; exit 1 ;;
esac

require_cmd curl
require_cmd jq
require_cmd awk

# Normalise specs: empty -> default, validate every entry is 0-100
for i in "${!SLUGS[@]}"; do
  spec="${SPECS[$i]}"
  [[ -z "$spec" ]] && spec="$THRESHOLDS_DEFAULT"
  for t in ${spec//,/ }; do
    if ! [[ "$t" =~ ^[0-9]+(\.[0-9]+)?$ ]] || [[ $(awk -v v="$t" 'BEGIN{print (v<0||v>100)?1:0}') == 1 ]]; then
      echo "Error: invalid threshold '$t' for slug '${SLUGS[$i]}' (0-100 expected)." >&2
      exit 1
    fi
  done
  SPECS[$i]="$spec"
done

# --- state ------------------------------------------------------------------
HAS_STATE=0
if [[ $NO_STATE -eq 0 ]]; then
  mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null
  [[ -f "$STATE_FILE" ]] || echo '{"slugs":{}}' > "$STATE_FILE" 2>/dev/null
  [[ -s "$STATE_FILE" ]] && HAS_STATE=1
fi

state_get_fired() {   # $1=slug -> newline separated fired thresholds
  [[ $HAS_STATE -eq 1 ]] || return 0
  jq -r --arg s "$1" '(.slugs[$s].fired // [])[]' "$STATE_FILE" 2>/dev/null
}

state_put() {         # $1=slug $2=fired-json-array $3=percent $4=thresholds-json
  [[ $HAS_STATE -eq 1 ]] || return 0
  local tmp
  tmp="$(mktemp "$(dirname "$STATE_FILE")/.state.XXXXXX")" || return 0
  if jq --arg s "$1" --argjson fired "$2" --argjson pct "$3" \
        --argjson thresholds "$4" --arg ts "$(date -u +%FT%TZ)" \
        '.slugs[$s] = {fired:$fired, last:$pct, thresholds:$thresholds, ts:$ts}' \
        "$STATE_FILE" > "$tmp" 2>/dev/null; then
    mv "$tmp" "$STATE_FILE"
  else
    rm -f "$tmp"
  fi
}

emoji_for() { # $1=percent
  awk -v p="$1" 'BEGIN{
    if      (p < 20) print "🟢"
    else if (p < 40) print "🟡"
    else if (p < 60) print "🟠"
    else if (p < 80) print "🔴"
    else             print "⚫"
  }'
}

tier_text() { # $1=percent
  awk -v p="$1" 'BEGIN{
    if      (p < 20) printf "No. The chance is very low (%s%%).", p
    else if (p < 40) printf "Unlikely. The chance is somewhat low (%s%%).", p
    else if (p < 60) printf "Maybe. The chance is moderate (%s%%).", p
    else if (p < 80) printf "Likely. The chance is somewhat high (%s%%).", p
    else             printf "Yes. The chance is very high (%s%%).", p
  }'
}

# --- per-market check -------------------------------------------------------
rc=0

for i in "${!SLUGS[@]}"; do
  slug="${SLUGS[$i]}"
  spec="${SPECS[$i]}"

  response="$(curl -sS --max-time 20 "${API_ENDPOINT}/markets/slug/${slug}" 2>/dev/null)"
  if [[ -z "$response" ]]; then
    echo "Error: no response from Polymarket API for '$slug'" >&2
    rc=1; continue
  fi

  parsed="$(jq -c '
    def n: if type == "number" then . elif type == "string" then (tonumber? // null) else null end;
    { q: (.question // null),
      bid: (.bestBid | n),
      ask: (.bestAsk | n),
      last: (.lastTradePrice | n),
      closed: (.closed // false) }' <<<"$response" 2>/dev/null)"

  if [[ -z "$parsed" || "$parsed" == "null" ]]; then
    echo "Error: unparseable API response for '$slug': $(tr -d '\n' <<<"$response" | cut -c1-200)" >&2
    rc=1; continue
  fi

  question="$(jq -r '.q // empty' <<<"$parsed")"
  if [[ -z "$question" ]]; then
    echo "Error: no market found for slug '$slug'" >&2
    rc=1; continue
  fi

  bid="$(jq -r '.bid // empty' <<<"$parsed")"
  ask="$(jq -r '.ask // empty' <<<"$parsed")"
  last="$(jq -r '.last // empty' <<<"$parsed")"
  closed="$(jq -r '.closed' <<<"$parsed")"

  price="$(awk -v b="$bid" -v a="$ask" -v l="$last" -v src="$PRICE_SOURCE" 'BEGIN{
    has_b=(b!=""); has_a=(a!=""); has_l=(l!="");
    mid = (has_b && has_a) ? (b+a)/2 : (has_a ? a : (has_b ? b : ""));
    if (src=="ask")       p = has_a ? a : (has_l ? l : mid);
    else if (src=="last") p = has_l ? l : mid;
    else                  p = mid;
    if (p == "") { print ""; exit }
    printf "%.4f", p*100
  }')"

  if [[ -z "$price" ]]; then
    echo "Error: no usable price for '$slug' (bid='$bid' ask='$ask' last='$last')" >&2
    rc=1; continue
  fi

  pct="$(awk -v p="$price" 'BEGIN{printf "%.1f", p}')"
  emoji="$(emoji_for "$pct")"
  tier="$(tier_text "$pct")"

  # Status line (stdout is discarded by cron; useful when run by hand)
  echo "$question"
  echo "${emoji} ${tier}"
  [[ "$closed" == "true" ]] && echo "   (market is closed)"
  echo "   slug=$slug price=${pct}% source=$PRICE_SOURCE thresholds=$spec"

  # --- threshold crossing logic ---------------------------------------------
  fired_now="$(state_get_fired "$slug")"
  new_hits=()
  kept=()

  for t in ${spec//,/ }; do
    already=0
    while IFS= read -r f; do
      [[ -z "$f" ]] && continue
      [[ $(awk -v v="$f" -v w="$t" 'BEGIN{print (v==w)?1:0}') == 1 ]] && already=1
    done <<<"$fired_now"

    if [[ $(awk -v p="$pct" -v t="$t" 'BEGIN{print (p>=t)?1:0}') == 1 ]]; then
      if [[ $already -eq 1 ]]; then
        kept+=("$t")
      else
        new_hits+=("$t"); kept+=("$t")
      fi
    elif [[ $already -eq 1 ]]; then
      # stays fired unless the price has fallen hysteresis points back below
      [[ $(awk -v p="$pct" -v t="$t" -v h="$HYSTERESIS" 'BEGIN{print (p <= t-h)?1:0}') == 1 ]] || kept+=("$t")
    fi
  done

  if [[ ${#new_hits[@]} -gt 0 ]]; then
    hits_sorted="$(printf '%s\n' "${new_hits[@]}" | sort -n | paste -sd ',' -)"

    msg="$question"$'\n'"${emoji} ${tier}"$'\n'
    while IFS= read -r t; do
      msg+="📈 Crossed ${t}% threshold"$'\n'
    done < <(printf '%s\n' "${new_hits[@]}" | sort -n)
    msg+="https://polymarket.com/market/${slug}"

    if [[ $DRY_RUN -eq 1 ]]; then
      echo "--- DRY RUN, would send ---"
      echo "$msg"
      echo "---------------------------"
    else
      require_cmd signal-cli
      if signal-cli -a "$SIGNAL_SENDER" send -m "$msg" "$SIGNAL_RECIPIENT" >/dev/null 2>&1; then
        echo "   alerted: crossed ${hits_sorted}%"
      else
        echo "Error: signal-cli failed for '$slug'" >&2
        rc=1
        continue   # do not mark as fired when the send failed
      fi
    fi
  fi

  # persist state
  kept_json="$(printf '%s\n' "${kept[@]:-}" | grep -v '^$' | sort -n | jq -R . | jq -cs . 2>/dev/null)"
  [[ -z "$kept_json" ]] && kept_json='[]'
  spec_json="$(printf '%s\n' ${spec//,/ } | jq -R . | jq -cs . 2>/dev/null)"
  [[ -z "$spec_json" ]] && spec_json='[]'
  state_put "$slug" "$kept_json" "$pct" "$spec_json"
done

exit $rc
