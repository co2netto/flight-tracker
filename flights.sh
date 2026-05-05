#!/data/data/com.termux/files/usr/bin/bash
# flights.sh - run the flight check
# Reads token from secrets.sh (which is git-ignored)
cd "$(dirname "$0")"

if [ ! -f secrets.sh ]; then
    echo "ERROR: secrets.sh not found. Create it with your Telegram credentials:"
    echo '  echo export TELEGRAM_BOT_TOKEN=\"your_token\" > secrets.sh'
    echo '  echo export TELEGRAM_CHAT_ID=\"your_chat_id\" >> secrets.sh'
    exit 1
fi

source secrets.sh
python check.py
