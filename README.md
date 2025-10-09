# polymarket-alert

A script to monitor Polymarket markets and send alerts via Signal when certain conditions are met. Only works with yes/no markets.

## Features

- Monitor specific Polymarket markets by slug.
- Send alerts to a specified Signal recipient when market conditions change.

## Requirements

- Bash shell
- `curl` for making HTTP requests
- `jq` for parsing JSON responses
- Signal CLI installed and configured

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/mariodian/polymarket-monitor.git
   cd polymarket-monitor
   ```
2. Ensure you have `curl`, `jq`, and Signal CLI installed on your system.
3. Configure Signal CLI with your phone number.

## Usage

Run the script with the required parameters:

```bash
./alert.sh --slug <market-slug> --signal-sender <signal-sender-number> --signal-recipient <signal-recipient-number>
```

### Parameters

- `--slug`: The slug of the Polymarket market to monitor (e.g., `will-polymarket-us-go-live-in-2025`).
- `--sender`: The phone number registered with Signal CLI to send messages from.
- `--recipient`: The phone number to receive the alerts.

### Example

```bash
./alert.sh --slug will-polymarket-us-go-live-in-2025 --sender +1234567890 --recipient +0987654321
```

## Help

For more information, run:

```bash
./alert.sh --help
```

## License

This project is unlicensed and free to use as you see fit.
