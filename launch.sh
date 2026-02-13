set -x
set -e
set -o

bash tokens_to_envs.sh from_env

ls ~/.ssh
mkdir -p ~/.ssh
set +x
echo "$GITHUB_SSH_PRIVATE" > ~/.ssh/id_ed25519
echo "$GITHUB_SSH_PUBLIC" > ~/.ssh/id_ed25519.pub
set -x
chmod 400 ~/.ssh/id_ed25519*
ssh-keyscan github.com >> ~/.ssh/known_hosts

# Configure git to accept github.com without strict host checking
cat > ~/.ssh/config << EOF
Host github.com
    StrictHostKeyChecking no
    IdentityFile ~/.ssh/id_ed25519
EOF

git clone -o origin git@github.com:aljernon/logseq.git ~/logseq
git clone -o origin git@github.com:aljernon/yarvis.git ~/repo
git clone -o origin git@github.com:aljernon/memory.git ~/core_knowledge

# Setup repository and pre-commit hooks
(
    cd ~/repo
    ln -s /app/core_knowledge
    # Install and setup pre-commit properly
    pip install pre-commit
    pre-commit install
)

git config --global pull.rebase false
git config --global user.email "you@example.com"
git config --global user.name "Yarvis Bot"

updater_logseq() {
	( cd ~/logseq && git pull )
	sleep $(( 5 * 60 ))
}

updater_logseq &

updater_core_knowledge() {
	( cd ~/core_knowledge && git pull )
	sleep $(( 5 * 60 ))
}

updater_core_knowledge &

export CUSTOM_TELEGRAM_BOT_PORT=12345

echo Run location logger
python location_logger.py &
echo Run bot
exec python telegram-claude-bot.py
