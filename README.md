# Stock Watchlist Bot

Free NSE (India) sector watchlist bot. Runs on **GitHub Actions** (no local install required) and sends:

1. A **2×/day digest** (morning + evening IST, weekdays)
2. **Near-real-time alerts** every ~20 minutes during market hours

All delivery is via **Telegram**. Data comes from free sources only: **yfinance** (prices) and **Google News RSS** (headlines). No paid APIs.

> GitHub’s scheduled cron can be delayed by a few minutes (sometimes longer) under load — that is normal.

---

## What you get

| Mode | When | Contents |
|------|------|----------|
| `digest` | 08:00 & 18:00 IST (Mon–Fri) | NIFTY 50 line, biggest movers, sector scoreboard, per-sector prices + 1–2 headlines, flags for 52w / volume |
| `monitor` | every 20 min ~08:30–16:30 IST | New news, deal-keyword 🚨 alerts, ± moves, volume spikes, near 52-week high/low |

---

## Project layout

```
stock-watchlist-bot/
  watchlist.yaml          # sectors + stocks + Google News queries
  config.yaml             # thresholds, market hours, alert toggles
  requirements.txt
  src/                    # Python package
  state/                  # seen_news.json, last_prices.json (updated by monitor)
  .github/workflows/      # digest.yml, monitor.yml
```

---

## Local dry-run (optional)

```bash
pip install -r requirements.txt
python -m src.main --mode digest --dry-run
python -m src.main --mode monitor --dry-run
```

`--dry-run` prints messages and does **not** send to Telegram or write state.

---

## Browser-only setup guide (no coding required)

Follow these five steps in any web browser. Everything is free.

### 1) Create a Telegram bot and get the token

1. Open Telegram and search for **`@BotFather`** (official blue checkmark).
2. Start a chat and send: `/newbot`
3. Choose a **display name** (e.g. `My Stock Watchlist`).
4. Choose a **username** ending in `bot` (e.g. `my_nse_watchlist_bot`).
5. BotFather replies with a long token that looks like `123456789:AAH...`  
   **Copy and save this** — this is your `TELEGRAM_BOT_TOKEN`.  
   Keep it private; anyone with the token can control your bot.

### 2) Get your `TELEGRAM_CHAT_ID`

1. In Telegram, search for your new bot by its username and tap **Start** (or send any message like `hi`).
2. In the same browser or phone browser, open this URL (paste your token in place of `YOUR_TOKEN`):

   `https://api.telegram.org/botYOUR_TOKEN/getUpdates`

3. Look in the JSON for a section like `"chat":{"id": 987654321`.  
   That number (can be negative for groups) is your **`TELEGRAM_CHAT_ID`**.
4. If you see `"result":[]`, send another message to the bot and refresh the URL.

**Tip:** You can also message `@userinfobot` or `@getidsbot` on Telegram; they reply with your numeric ID.

### 3) Create a public GitHub repo and upload these files

1. Sign in at [https://github.com](https://github.com) (create a free account if needed).
2. Click **+** (top right) → **New repository**.
3. Name it `stock-watchlist-bot`, set visibility to **Public**, do **not** add a README if you will upload this folder as-is, then click **Create repository**.
4. On the empty repo page, click **uploading an existing file** (or **Add file** → **Upload files**).
5. Drag **all** project files and folders into the browser (including `.github`, `src`, `state`, YAML files, etc.).  
   Make sure hidden folders like `.github` are included — on some systems you may need to upload from a zip: zip the project, then on GitHub use upload, or use **Add file** multiple times for each folder.
6. Click **Commit changes**.

**Easier alternative:** On the new repo page, click **creating a new file**, but uploading a zip via the UI is simplest: create a zip of the project on your computer, unzip locally if needed, then drag the contents into GitHub’s upload page preserving folders.

### 4) Add the two Secrets

1. Open your repo on GitHub.
2. Go to **Settings** → **Secrets and variables** → **Actions**.
3. Click **New repository secret**.
4. Name: `TELEGRAM_BOT_TOKEN` — paste the BotFather token → **Add secret**.
5. Click **New repository secret** again.
6. Name: `TELEGRAM_CHAT_ID` — paste your chat id → **Add secret**.

### 5) Enable Actions and run a test digest

1. Open the **Actions** tab of your repo.
2. If prompted, click **I understand my workflows, go ahead and enable them**.
3. In the left sidebar, click **Digest**.
4. Click **Run workflow** → **Run workflow** (branch `main`).
5. Wait 1–2 minutes, then open the run and confirm it is green.
6. Check Telegram — you should receive the sector digest.

Then optionally run **Monitor** the same way (Actions → Monitor → Run workflow).  
After monitor runs, `state/seen_news.json` and `state/last_prices.json` may get new commits from `github-actions[bot]` — that is expected (anti-spam / dedupe).

---

## Config knobs (`config.yaml`)

- `price_move_threshold_pct` / `re_alert_step_pct` — price alert sensitivity
- `volume_spike_ratio` — volume vs 20-day average
- `near_52w_pct` — distance to 52-week high/low
- `alerts.enable_*` — toggle each of the five alert types
- `deal_keywords` — headlines that become 🚨 HIGH PRIORITY
- `market_hours` — Asia/Kolkata session used to skip price/volume/52w when closed (news still allowed)

Edit `watchlist.yaml` to add/remove stocks (use NSE symbols; the bot appends `.NS` for Yahoo Finance).

---

## License

MIT — use freely for personal market monitoring.
