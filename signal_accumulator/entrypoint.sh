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

# Start accumulator connecting to localhost
export SIGNAL_WS_URL=ws://localhost:8080
export SIGNAL_DB_PATH=/data/signal_messages.db
exec /opt/accumulator-venv/bin/python /opt/accumulator/accumulator.py &
ACCUM_PID=$!

# If either process exits, stop the other
trap "kill $SIGNAL_PID $ACCUM_PID 2>/dev/null; exit" SIGTERM SIGINT

wait -n $SIGNAL_PID $ACCUM_PID
echo "One process exited, shutting down..."
kill $SIGNAL_PID $ACCUM_PID 2>/dev/null
wait
