

Installing hooks:

https://api.telegram.org/bot<XXX>/setWebhook?url=https://claude-telegram-c4fccbf117d9.herokuapp.com/<YYY>


### Database

Using essential tier of posgres:

```
heroku addons:create heroku-postgresql:essential-0
```

Needed to install postgres locally
```
brew install postgresql
```

Now can connect:

```
heroku pg:psql --app claude-telegram
```


To talk to it locally (this command also creates the tables):
```
heroku pg:credentials:url DATABASE
# copy the database URL
DATABASE_URL=postgres://ucl9gubtr_BLA_BAL python storage.py
```

Or one-liner:
```
export DATABASE_URL=$(heroku config | grep DAT | awk '{print $2}')
```


This should create all needed tables:
```
python -m yarvis_ptb.storage
```


This kills all previous requests:
```
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE pid <> pg_backend_pid()
AND datname = current_database();
```


### Google keys

Need to create app and give accesses

https://console.cloud.google.com/apis/credentials/oauthclient/174182349243-v6jt7ih0k47f0bke74quichekenkee95.apps.googleusercontent.com?inv=1&invt=Able9g&project=silken-math-445707-v4

For adding API keys

scopes:
"https://www.googleapis.com/auth/keep"
'https://www.googleapis.com/auth/calendar'

Run

```
./update_tokens.sh
```

to get tokens




### Required heroku env vars:

```
# which settings in yarvis_ptb/settings/ to use.
SETTINGS_NAME=

# Anthropic API key
ANTHROPIC_API_KEY=

# Bot token. Not sure about hash and id.
TELEGRAM_BOT_TOKEN=

# For gkeep
ANDROID_ID=
KEEP_EMAIL=
MASTER_TOKEN=

# GH token for code editing. Not required.
GH_TOKEN=

# Github tokens to handle memory git repo.
GITHUB_SSH_PRIVATE=
GITHUB_SSH_PUBLIC=

# For voice recognition
B64_TOKEN_SESSION_NAME_SESSION=
TELEGRAM_HASH=
TELEGRAM_ID=

# Automatically set by ./update_tokens.sh
B64_TOKEN_GMAIL_TOKEN_JSON=
B64_TOKEN_SERVICE_ACCOUNT_JSON=
B64_TOKEN_TOKEN_PICKLE=
```
