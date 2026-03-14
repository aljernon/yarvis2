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

# Patch supervisor config: --receive-mode=on-connection delays receiving from
# Signal servers until a websocket client connects, preventing message loss
# during the startup gap before the accumulator is ready.
SUPERVISOR_CONF="/etc/supervisor/conf.d/signal-cli-json-rpc-1.conf"
if [ -f "$SUPERVISOR_CONF" ] && ! grep -q 'receive-mode' "$SUPERVISOR_CONF"; then
    sed -i 's/daemon /daemon --receive-mode=on-connection --send-read-receipts /' "$SUPERVISOR_CONF"
    echo "Patched supervisor config with --receive-mode=on-connection --send-read-receipts"
    supervisorctl reread
    supervisorctl restart signal-cli-json-rpc-1
    # Wait for signal-cli to be ready again after restart
    echo "Waiting for signal-cli to restart..."
    for i in $(seq 1 30); do
        if curl -sf http://localhost:8080/v1/about > /dev/null 2>&1; then
            echo "signal-cli-rest-api is ready after restart"
            break
        fi
        sleep 1
    done
fi

# Start accumulator connecting to localhost
export SIGNAL_WS_URL=ws://localhost:8080
export SIGNAL_DB_PATH=/data/signal_messages.db
/opt/accumulator-venv/bin/python /opt/accumulator/accumulator.py &
ACCUM_PID=$!

# If either process exits, stop the other
trap "kill $SIGNAL_PID $ACCUM_PID 2>/dev/null; exit" SIGTERM SIGINT

wait -n $SIGNAL_PID $ACCUM_PID
echo "One process exited, shutting down..."
kill $SIGNAL_PID $ACCUM_PID 2>/dev/null
wait
