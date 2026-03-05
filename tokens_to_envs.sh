#!/bin/bash
# Read tokens into env vars in heroku:
#    ./update_tokens.sh to_env
# Read env vars from env and save to files (to be executed on heroku:
#    ./update_tokens.sh from_env
set -e
set -u

# List of filenames
files=(
    "service_account.json"
    "credentials.json"
    "token.pickle"
    "gmail_token.json"
    "session_name2.session"
    "whoop_config.json"
    "whoop_token.json"
    "nest_config.json"
    "nest_token.json"
)

# Loop through each file
for file in "${files[@]}"; do
    # Convert filename to env var name:
    # 1. Replace dots and hyphens with underscores
    # 2. Convert to uppercase
    env_var=B64_TOKEN_$(echo "$file" | tr '.-' '_' | tr '[:lower:]' '[:upper:]')

    if [[ "$1" = "to_env" ]]; then
        if [ -f "${file}" ]; then
            heroku config:set -a claude-telegram-v2  ${env_var}="$(cat $file | gzip | base64)"
        else
            echo "Error: File $file does not exist"
        fi
    elif [[ "$1" = "from_env" ]]; then
        echo "Reading $env_var from environment to $file"
        if [ -z "${!env_var}" ]; then
            echo "Error: Environment variable $env_var is not set"
            continue
        fi
        echo ${!env_var} | base64 -d | gunzip > "$file"
    else
        echo "Error: Invalid argument $1"
        echo "Usage: $0 to_env|from_env"
        exit 1
    fi
done
