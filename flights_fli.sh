#!/data/data/com.termux/files/usr/bin/bash
# flights_fli.sh - run the fli-based flight check (parallel to flights.sh)
cd "$(dirname "$0")"

if [ ! -f secrets.sh ]; then
    echo "ERROR: secrets.sh not found."
    exit 1
fi

source secrets.sh
python check_fli.py
