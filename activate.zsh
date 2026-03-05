conda activate clam
#pip install -r requirements.txt
export DATABASE_URL=$(heroku config -a claude-telegram-v2 | grep DAT | awk '{print $2}')
export SETTINGS_NAME=anton
