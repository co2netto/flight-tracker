# Flight Tracker (Termux / Android)

On-demand flight price checker.
You run one command, it fetches real-time Google Flights prices for
all your routes, prints a summary, and sends it to Telegram.
History is kept locally so you get trend arrows (🟢↓ / 🔴↑ / ⚪→) vs. the
previous run.

**Data source:** Google Flights (via the `fast-flights` library) — real-time,
no API key, free.

**Tradeoff:** Google Flights scraping can break when Google updates their
site. If a run returns "no data" for everything, run
`pip install --upgrade fast-flights` and try again.

## One-time Android setup (~10 minutes)

### 1. Install Termux
Install from **F-Droid** (not Play Store — the Play Store version is
outdated): https://f-droid.org/en/packages/com.termux/

### 2. Inside Termux, install Python and git
```bash
pkg update && pkg upgrade -y
pkg install python git -y
```

### 3. Get the code onto your phone
Easiest route — put these 4 files into a GitHub repo, then:
```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git flight-tracker
cd flight-tracker
```

(Or transfer the files via any method you like and `cd` into the folder.)

### 4. Install the Python dependencies
```bash
pip install -r requirements.txt
```

### 5. Fill in your Telegram credentials
Open `flights.sh` with any editor (nano is already installed):
```bash
nano flights.sh
```
Replace the two `PASTE_YOUR_..._HERE` values with your real bot token
and chat ID. Save with `Ctrl+O`, Enter, `Ctrl+X`.

Make it executable:
```bash
chmod +x flights.sh
```

### 6. Test it
```bash
./flights.sh
```
You should see each route being checked in the terminal, then a summary,
and get the same summary on Telegram.

## Daily use

Open Termux, then:
```bash
cd flight-tracker
./flights.sh
```

That's the whole loop. Takes ~30–60 seconds for 8 routes.

## Tips

- **Faster launch:** Termux supports widgets. Install "Termux:Widget"
  from F-Droid, then create a shortcut script in `~/.shortcuts/` that
  runs `./flights.sh`. You get a home-screen button.
- **Editing routes:** open `check.py`, change the `ROUTES` list, save.
- **Currency:** Google Flights returns prices in your locale's currency.
  If you want THB, set your Google account region to Thailand. The
  `CURRENCY_LABEL` in `check.py` is only used for display.
- **History:** stored in `price_history.json` in the same folder. Delete
  it to reset trend tracking.

## Files

- `check.py` — main script
- `flights.sh` — convenience launcher with env vars
- `requirements.txt` — Python deps
- `.gitignore` — keeps junk out of git
- `price_history.json` — auto-created on first run
