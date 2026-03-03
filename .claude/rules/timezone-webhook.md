# Timezone Webhook

Automatic timezone updates from Android Tasker.

## Architecture
- **Endpoint**: `POST /api/timezone` in `yarvis_ptb/yarvis_ptb/webhook_handlers.py` (`TimezoneHandler`)
- **Registered in**: `telegram-claude-bot.py`
- **Auth**: Bearer token via `WEBHOOK_SECRET` env var (`Authorization: Bearer {secret}`)
- **Timezone logic**: `yarvis_ptb/yarvis_ptb/timezones.py` — `set_timezone()` updates `core_knowledge/settings.json`
- **System message**: On change, saves a SYSTEM_USER_ID message noting old → new timezone

## Request Format
```json
POST /api/timezone
Authorization: Bearer <WEBHOOK_SECRET>
{"timezone": "America/Los_Angeles"}
```
Timezone is validated via `pytz.timezone()` before acceptance.
