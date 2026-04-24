#!/data/data/com.termux/files/usr/bin/bash
# Convenience wrapper — edit the two export lines below once, then just run ./flights.sh
cd "$(dirname "$0")"
export TELEGRAM_BOT_TOKEN="8687448446:AAEQz4jGcwNMbrHwa5EJBhZunrtNpctGL4M"
export TELEGRAM_CHAT_ID="1346776894"
python check.py
