# Signal Accumulator

Combined container: `bbernhard/signal-cli-rest-api` + Python accumulator in one Docker image. Both processes start/stop together, eliminating message loss from accumulator websocket disconnects.

## Architecture

- `signal-cli-rest-api` (Java) runs in JSON-RPC mode, receives messages from Signal servers
- `accumulator.py` (Python/Flask) connects to it via `ws://localhost:8080`, stores messages in SQLite
- Both run in the same container via `entrypoint.sh` — if either exits, the other is stopped
- Signal number: `+16506603785`

## Key files

- `accumulator.py` — websocket listener, SQLite storage, Flask API
- `Dockerfile.combined` — combined image (base: signal-cli-rest-api + Python + accumulator)
- `Dockerfile` — standalone accumulator only (legacy, for separate-container setup)
- `entrypoint.sh` — starts signal-cli in background, waits for ready, starts accumulator
- `requirements.txt` — Flask + websocket-client

## HTTP API

- `GET /messages?hours=24&source=...&limit=100` — query messages
- `GET /health` — websocket connection status

## Deployment

Runs on GCP VM (`signal-api`, `100.108.7.78`).

```bash
# Copy files to VM
gcloud compute scp signal_accumulator/* signal-api:~/signal_accumulator/ --zone us-central1-a --tunnel-through-iap

# On the VM: build and redeploy
cd ~/signal_accumulator
sudo docker build -f Dockerfile.combined -t signal-combined .
sudo docker stop signal-combined signal-accumulator signal-connection-server 2>/dev/null
sudo docker rm signal-combined signal-accumulator signal-connection-server 2>/dev/null
sudo docker run -d --name signal-combined \
  --restart=unless-stopped \
  -e MODE=json-rpc \
  -p 100.108.7.78:8080:8080 \
  -p 100.108.7.78:8081:8081 \
  -v signal-cli-data:/home/.local/share/signal-cli \
  -v signal-accumulator-data:/data \
  signal-combined

# Verify
sudo docker logs --tail 20 signal-combined
```

Note: this replaces BOTH the old `signal-connection-server` and `signal-accumulator` containers.
The signal-cli data volume (`signal-cli-data`) is reused from the old setup.
