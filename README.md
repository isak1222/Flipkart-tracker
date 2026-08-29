# Flipkart Price + Bajaj EMI Tracker (Telegram bot)

## What it does
- `/add <flipkart_url> <target_price>` — start tracking a product
- Checks every 15 min (configurable) for price changes → sends a Telegram message on every change
- **Bajaj EMI: not auto-detected.** Flipkart loads EMI options via a client-side JS call, not the static page this bot fetches — confirmed via live traffic inspection on 2026-08-30. Rather than show a possibly-false ✅/❌, every message just reminds you to check manually on the product page. If you want this automated later, the fix is finding Flipkart's internal EMI API endpoint and calling it directly (see git history / ask me).
- When price ≤ your target, it starts an **escalating alarm**: normal pings every 60s for the first ~5 min, then bursts of 3 rapid "WAKE UP" messages every 30s — until you send `/ack <id>`. This is the free equivalent of a "hard notification"; multiple back-to-back notification sounds have a better shot at waking you than a single message.
  - **For this to actually wake you while asleep**, also check your phone's notification settings: Telegram app notifications must not be muted, and ideally set Telegram (or this specific chat) to override Do Not Disturb/silent mode — this is a phone setting, not something the bot can control. On Android: Settings → Apps → Telegram → Notifications → make sure the bot's chat channel can bypass DND.
- `/list`, `/remove <id>`, `/check <id>` (force an immediate check)


## 1. Create your Telegram bot
1. Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` → follow prompts
2. Copy the token it gives you (looks like `123456:ABC-xyz...`)
3. Message your new bot once so it can find your chat — then visit
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser to find your `chat.id` (optional, only needed if you want to lock the bot to just you via `ALLOWED_CHAT_ID`)

## 2. Run locally to test first (recommended before deploying)
```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="your_token_here"
python bot.py
```
Then in Telegram: `/add https://www.flipkart.com/... 55000`

**Test this before deploying** — Flipkart scraping is the fragile part. If `/add` fails to fetch a price, check the error message; you may need to tweak the CSS selectors in `scraper.py` (Flipkart changes their class names periodically).

## 3. Deploy to Render
1. Push this folder to a GitHub repo
2. Render dashboard → New → **Web Service** → connect the repo
3. Build command: `pip install -r requirements.txt`
4. Start command: `python bot.py`
5. Add environment variables (Render dashboard → Environment):
   - `TELEGRAM_BOT_TOKEN` = your bot token
   - `ALLOWED_CHAT_ID` = your chat id (optional, recommended so randoms can't use your bot)
   - `CHECK_INTERVAL_MINUTES` = `15` (or whatever you want)
   - `ALARM_REPEAT_SECONDS` = `60`
6. Deploy. Render's free tier needs an HTTP port bound — `keep_alive.py` handles that.

## Known limitations (read before relying on this for a BBD sale)
- **Render free tier has an ephemeral filesystem** — every redeploy wipes `tracker.db`, so your tracked list resets. For BBD week, either don't redeploy after adding your products, or upgrade to Render's persistent disk (~$1/mo), or I can swap SQLite for a free hosted Postgres (Supabase/Neon) so it survives redeploys — say the word if you want that instead.
- **Flipkart scraping can break or get blocked.** No official product API exists. If Flipkart starts returning captcha pages to Render's IP, the price checks will silently fail (you'll only notice via `/check` errors). Test it live for a day before the sale starts.
- **Bajaj EMI detection is a text-search on the raw HTML.** If Flipkart loads the EMI widget via JavaScript after page load (common for EMI details), this will show ❌ even when it's actually available on the live page — you'd need a headless browser (Playwright) to catch that, which is heavier to run on Render's free tier. Flag it to me if this happens and I'll upgrade it.
- **Render free tier spins down after ~15 min of no traffic** and takes ~30s to wake on the next request — the self-ping keep-alive helps but isn't bulletproof; an external uptime pinger (e.g. cron-job.org, free) hitting your Render URL every 10 min is the reliable fix.
