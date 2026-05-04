#!/data/data/com.termux/files/usr/bin/bash
# sync.sh - pull latest code from GitHub, preserve local flights.sh, notify Telegram
cd "$(dirname "$0")"

# Read Telegram creds from flights.sh without executing the python call
TELEGRAM_BOT_TOKEN=$(grep TELEGRAM_BOT_TOKEN flights.sh | head -1 | cut -d'"' -f2)
TELEGRAM_CHAT_ID=$(grep TELEGRAM_CHAT_ID flights.sh | head -1 | cut -d'"' -f2)

# Backup flights.sh (it has the token, never goes to GitHub)
cp flights.sh /tmp/flights.sh.bak

# Discard any local edits to tracked files so pull doesn't conflict
git checkout -- check.py 2>/dev/null
git checkout -- flights.sh 2>/dev/null

# Pull
RESULT=$(git pull 2>&1)

# Restore flights.sh
cp /tmp/flights.sh.bak flights.sh
chmod +x flights.sh

# Notify Telegram
curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  -d "chat_id=${TELEGRAM_CHAT_ID}" \
  --data-urlencode "text=Sync from GitHub: ${RESULT}" > /dev/null

echo "$RESULT"