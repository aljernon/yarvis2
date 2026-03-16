#!/bin/bash
set -e

# Start signal-cli-rest-api in background (uses the base image's entrypoint)
/entrypoint.sh &
SIGNAL_PID=$!

# Wait for signal-cli API to be ready
echo "Waiting for signal-cli-rest-api..."
for i in $(seq 1 60); do
    if curl -sf http://localhost:8080/v1/about > /dev/null 2>&1; then
        echo "signal-cli-rest-api is ready"
        break
    fi
    sleep 1
done

# Clean up any leftover supervisor patches from previous deploys
SUPERVISOR_CONF="/etc/supervisor/conf.d/signal-cli-json-rpc-1.conf"
NEEDS_RESTART=0
if [ -f "$SUPERVISOR_CONF" ] && grep -q 'send-read-receipts\|receive-mode' "$SUPERVISOR_CONF"; then
    sed -i 's/ --send-read-receipts//; s/ --receive-mode=on-connection//' "$SUPERVISOR_CONF"
    echo "Cleaned up supervisor config flags"
    NEEDS_RESTART=1
fi
if [ "$NEEDS_RESTART" = "1" ]; then
    supervisorctl reread
    supervisorctl restart signal-cli-json-rpc-1
    echo "Waiting for signal-cli to restart..."
    for i in $(seq 1 30); do
        if curl -sf http://localhost:8080/v1/about > /dev/null 2>&1; then
            echo "signal-cli-rest-api is ready after cleanup"
            break
        fi
        sleep 1
    done
fi

# Start accumulator connecting to localhost
export SIGNAL_WS_URL=ws://localhost:8080
export SIGNAL_DB_PATH=/data/signal_messages.db
export PYTHONUNBUFFERED=1
/opt/accumulator-venv/bin/python /opt/accumulator/accumulator.py &
ACCUM_PID=$!

# If either process exits, stop the other
trap "kill $SIGNAL_PID $ACCUM_PID 2>/dev/null; exit" SIGTERM SIGINT

wait -n $SIGNAL_PID $ACCUM_PID
echo "One process exited, shutting down..."
kill $SIGNAL_PID $ACCUM_PID 2>/dev/null
wait
