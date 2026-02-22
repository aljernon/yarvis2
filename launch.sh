set -x
set -e
set -o

# Start Tailscale (userspace networking for Heroku - no /dev/net/tun)
if [ -n "$TAILSCALE_AUTH_KEY" ]; then
    echo "$(date) START tailscale setup"
    curl -fsSL https://pkgs.tailscale.com/stable/tailscale_1.78.1_amd64.tgz | tar xzf - -C /tmp/
    /tmp/tailscale_1.78.1_amd64/tailscaled --state=/tmp/tailscaled.state --tun=userspace-networking --socks5-server=localhost:1055 &
    sleep 2
    /tmp/tailscale_1.78.1_amd64/tailscale up --auth-key="$TAILSCALE_AUTH_KEY" --hostname=yarvis-heroku
    export ALL_PROXY=socks5://localhost:1055/
    echo "$(date) DONE tailscale setup"
fi

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

echo "$(date) START clone logseq"
git clone -o origin git@github.com:aljernon/logseq.git ~/logseq
echo "$(date) DONE clone logseq"

echo "$(date) START clone yarvis"
git clone -o origin git@github.com:aljernon/yarvis.git ~/repo
echo "$(date) DONE clone yarvis"

echo "$(date) START clone memory"
git clone -o origin git@github.com:aljernon/memory.git ~/core_knowledge
echo "$(date) DONE clone memory"

# Setup repository and pre-commit hooks
echo "$(date) START repo setup"
(
    cd ~/repo
    ln -s /app/core_knowledge
    # Install and setup pre-commit properly
    pip install pre-commit
    pre-commit install
)
echo "$(date) DONE repo setup"

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

# location_logger.py was a Flask proxy that bound to $PORT (for Heroku health check)
# and forwarded to the bot on CUSTOM_TELEGRAM_BOT_PORT. Without it, the bot must
# bind to $PORT directly, so don't set CUSTOM_TELEGRAM_BOT_PORT.
#export CUSTOM_TELEGRAM_BOT_PORT=12345
#python location_logger.py &

echo "$(date) START bot"
exec python telegram-claude-bot.py
