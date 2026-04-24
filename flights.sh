#!/data/data/com.termux/files/usr/bin/bash
# Convenience wrapper — edit the two export lines below once, then just run ./flights.sh
cd "$(dirname "$0")"
export TELEGRAM_BOT_TOKEN=""
export TELEGRAM_CHAT_ID=""
python check.py
