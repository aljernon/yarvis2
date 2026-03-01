# Whoop Integration

## Overview
Yarvis has a `get_whoop_data` tool that fetches health data from the Whoop API v2. It's registered as part of `ANTON_DATA_TOOLS` in `tool_sampler.py` (under the `"anton_google"` tool class).

## Architecture
- **No external library** — uses direct HTTP calls to `https://api.prod.whoop.com/developer/v2` via `requests`
- **Tool file**: `yarvis_ptb/yarvis_ptb/tools/whoop_tools.py`
- **Auth script**: `whoop_auth.py` (one-time OAuth2 flow, run locally)

## Credential Files (project root, gitignored)
- `whoop_config.json` — client_id, client_secret, redirect_uri
- `whoop_token.json` — access_token, refresh_token, expires_in, scopes, created_at

## API Endpoints (v2)
| data_type   | API path             |
|-------------|----------------------|
| recovery    | `/recovery`          |
| sleep       | `/activity/sleep`    |
| workouts    | `/activity/workout`  |
| cycles      | `/cycle`             |

**Important**: v1 endpoints only work for `/cycle`. Sleep, workouts, and recovery require v2 paths. The `whoopy` Python library uses wrong paths internally — that's why we use direct API calls instead.

## Token Refresh
- Tokens expire after 1 hour
- Auto-refresh is implemented in `_refresh_token()` using the refresh token + client credentials from `whoop_config.json`
- Current limitation: the initial OAuth grant may not return a refresh token (Whoop API behavior). If token expires without a refresh token, re-run `whoop_auth.py`

## Re-authentication
```bash
# Requires WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET in .env
python whoop_auth.py
```
This opens a browser for OAuth consent, catches the callback on `localhost:8765/callback`, and saves both config and token files.

## Heroku Deployment
Token files are shipped to Heroku via `tokens_to_envs.sh` (same as Google credentials):
- Files are gzipped + base64'd into Heroku config vars (`B64_TOKEN_WHOOP_CONFIG_JSON`, `B64_TOKEN_WHOOP_TOKEN_JSON`)
- Restored at runtime via `tokens_to_envs.sh from_env`
- To push updated tokens: `./tokens_to_envs.sh to_env` (or `./update_tokens.sh` which also refreshes Google tokens)
