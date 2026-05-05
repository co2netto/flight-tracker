#!/data/data/com.termux/files/usr/bin/bash
# sync.sh - pull latest code from GitHub and notify Telegram
# secrets.sh is git-ignored so flights.sh can be overwritten safely
cd "$(dirname "$0")"

source secrets.sh 2>/dev/null

RESULT=$(git pull 2>&1)
chmod +x flights.sh sync.sh 2>/dev/null

if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      -d "chat_id=${TELEGRAM_CHAT_ID}" \
      --data-urlencode "text=Sync from GitHub: ${RESULT}" > /dev/null
fi

echo "$RESULT"
