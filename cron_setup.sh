#!/bin/bash
# Install the cron job to run StockAdvisor at 6:00 AM PST (14:00 UTC) on weekdays.
# Run this once: bash cron_setup.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(which python3)"
LOG="$SCRIPT_DIR/logs/stock_advisor.log"

# Ensure log directory exists
mkdir -p "$SCRIPT_DIR/logs"

CRON_LINE="0 14 * * 1-5 cd $SCRIPT_DIR && $PYTHON stock_advisor.py >> $LOG 2>&1"

# Remove any existing stock_advisor cron entry, then add the new one
(crontab -l 2>/dev/null | grep -v "stock_advisor"; echo "$CRON_LINE") | crontab -

echo "Cron job installed:"
echo "  $CRON_LINE"
echo ""
echo "Logs will be written to: $LOG"
echo "To verify: crontab -l | grep stock_advisor"
