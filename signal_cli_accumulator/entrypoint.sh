#!/bin/bash
set -e

export SIGNAL_DB_PATH=${SIGNAL_DB_PATH:-/data/signal_messages.db}
export SIGNAL_CLI_PATH=signal-cli
export SIGNAL_CLI_CONFIG=/signal-cli-config
export SIGNAL_ACCOUNT=${SIGNAL_ACCOUNT:-+16506603785}
export VIEW_ONCE_ECHO_SENDERS=${VIEW_ONCE_ECHO_SENDERS:-}
export PYTHONUNBUFFERED=1

# Initialize the DB
/opt/accumulator-venv/bin/python /opt/accumulator/capture.py --init-db-only

# Set up cron for capture every 5 minutes
CRON_LOG=/data/capture.log
cat > /etc/cron.d/signal-capture << EOF
*/5 * * * * root SIGNAL_DB_PATH=$SIGNAL_DB_PATH SIGNAL_CLI_PATH=$SIGNAL_CLI_PATH SIGNAL_CLI_CONFIG=$SIGNAL_CLI_CONFIG SIGNAL_ACCOUNT=$SIGNAL_ACCOUNT VIEW_ONCE_ECHO_SENDERS=$VIEW_ONCE_ECHO_SENDERS PYTHONUNBUFFERED=1 /opt/accumulator-venv/bin/python /opt/accumulator/capture.py >> $CRON_LOG 2>&1
EOF
chmod 0644 /etc/cron.d/signal-capture

# Start cron daemon
service cron start
echo "Cron started — capture runs every 5 minutes"

# Run initial capture
echo "Running initial capture..."
/opt/accumulator-venv/bin/python /opt/accumulator/capture.py >> $CRON_LOG 2>&1 || true

# Start HTTP server (foreground — keeps container alive)
exec /opt/accumulator-venv/bin/python /opt/accumulator/serve.py
