#!/bin/bash
set -e
set -u


echo "Updating tokens"

rm -f token.pickle
python -m clam_ptb.tools.gmail_tool
sleep 5
python -m clam_ptb.tools.gcal_tools


./tokens_to_envs.sh "to_env"
