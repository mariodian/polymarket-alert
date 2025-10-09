#!/bin/bash

SCRIPT_NAME=./$(basename "$0")
API_ENDPOINT=https://gamma-api.polymarket.com
THRESHOLD="${THRESHOLD:-60}"

show_usage() {
  cat << EOF
A script to monitor Polymarket markets and send alerts via Signal when certain conditions are met.
Usage: $SCRIPT_NAME [OPTIONS]

Startup:
  -h --help                               print this help information
  -s --slug=SLUG                          Polymarket market slug (e.g. will-china-invade-taiwan-in-2025)
  -t --threshold=THRESHOLD                Threshold percentage to trigger alert (default: 60)

Signal notification:
  -ss --signal-sender=SENDER              Phone number of the sender. Needs signal-cli.
  -sr --signal-recipient=RECIPIENT        Phone number of the recipient. Needs signal-cli.

Example:
  $SCRIPT_NAME -s "will-polymarket-us-go-live-in-2025" -t 25 -ss +1234567890 --sr +0987654321
EOF
  exit 1
}

# Validate dependencies
require_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Error: '$cmd' is required but not installed." >&2
    exit 1
  fi
}

# Require dependencies
require_cmd curl
require_cmd jq

# Handle arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --slug=*|-s=*)
      SLUG="${1#*=}"
      shift
      ;;
    --slug|-s)
      SLUG="$2"
      shift 2
      ;;
    --threshold=*|-t=*)
      THRESHOLD="${1#*=}"
      shift
      ;;
    --threshold|-t)
      THRESHOLD="$2"
      shift 2
      ;;
    --signal-sender=*|-ss=*)
      SIGNAL_SENDER="${1#*=}"
      shift
      ;;
    --signal-sender|-ss)
      SIGNAL_SENDER="$2"
      shift 2
      ;;
    --signal-recipient=*|-sr=*)
      SIGNAL_RECIPIENT="${1#*=}"
      shift
      ;;
    --signal-recipient|-sr)
      SIGNAL_RECIPIENT="$2"
      shift 2
      ;;
    --help|-h)
      show_usage
      shift
      ;;
    *)
      echo "Unknown argument $1"
      shift
      exit 1
      ;;
  esac
done

# Validate required arguments
if [[ ! "$SLUG" ]]; then
  echo "Error: No market slug specified."
fi

if [[ ! "$SIGNAL_SENDER" ]]; then
  echo "Error: No signal sender specified."
fi

if [[ ! "$SIGNAL_RECIPIENT" ]]; then
  echo "Error: No signal recipient specified."
fi

if [[ ! "$SLUG" || ! "$SIGNAL_SENDER" || ! "$SIGNAL_RECIPIENT" ]]; then
  echo "See $SCRIPT_NAME --help"
  exit 1
fi

# Send request to Polymarket API
response="$(curl -sS "${API_ENDPOINT}/markets/slug/${SLUG}" || true)"

# Check for empty response
if [[ -z "$response" ]]; then
  echo "Error: No response from Polymarket API"
  exit 1
fi

# Parse response
question="$(echo "$response" | jq -r '.question')"
last_trade_price="$(echo "$response" | jq -r '.lastTradePrice')"
best_bid="$(echo "$response" | jq -r '.bestBid')"
best_ask="$(echo "$response" | jq -r '.bestAsk')"
implied_percent="$(echo "scale=2; ${best_ask} * 100" | bc)"

# Validate numeric values
if ! [[ "${best_bid}" =~ ^[0-9]+(\.[0-9]+)?$ ]] || ! [[ "${best_ask}" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
  # Print a compact preview for debugging
  preview="$(echo "${response}" | jq -c 'del(.bids,.asks) // .')"
  echo "[${ts}] Warning: Could not parse best bid/ask. Response preview: ${preview}"
  exit 1
fi

# Determine emoji and message based on implied percentage
if (( $(echo "$implied_percent >= 0" | bc -l) && $(echo "$implied_percent < 20" | bc -l) )); then
  emoji="🟢"
  message="No. The chance is very low (${implied_percent}%)."
elif (( $(echo "$implied_percent >= 20" | bc -l) && $(echo "$implied_percent < 40" | bc -l) )); then
  emoji="🟡"
  message="Unlikely. The chance is somewhat low (${implied_percent}%)."
elif (( $(echo "$implied_percent >= 40" | bc -l) && $(echo "$implied_percent < 60" | bc -l) )); then
  emoji="🟠"
  message="Maybe. The chance is moderate (${implied_percent}%)."
elif (( $(echo "$implied_percent >= 60" | bc -l) && $(echo "$implied_percent < 80" | bc -l) )); then
  emoji="🔴"
  message="Likely. The chance is somewhat high (${implied_percent}%)."
elif (( $(echo "$implied_percent >= 80" | bc -l) )); then
  emoji="⚫"
  message="Yes. The chance is very high (${implied_percent}%)."
else
  emoji="❓"
fi

# Print status
echo $question
echo "${emoji} ${message}"

# Send alert if threshold is exceeded
if (( $(echo "$implied_percent > $THRESHOLD" | bc -l) )); then
  require_cmd signal-cli

  signal-cli -a "${SIGNAL_SENDER}" send -m "${question}\n${emoji} ${message}" "${SIGNAL_RECIPIENT}"
fi

exit 0
