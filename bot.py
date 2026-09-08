"""
bot.py
Flipkart price + Bajaj EMI tracker Telegram bot.

Commands:
  /add <flipkart_url> <target_price>   - start tracking a product
  /list                                 - show all tracked products
  /remove <id>                          - stop tracking a product
  /ack <id>                             - silence an active "price hit" alarm
  /check <id>                           - force an immediate price check

Env vars required (set these in Render's dashboard, never hardcode them):
  TELEGRAM_BOT_TOKEN   - from @BotFather
  ALLOWED_CHAT_ID       - (optional) restrict the bot to your chat only
"""

import os
import logging
import asyncio
import html
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import storage
from scraper import fetch_product, ScrapeError
from keep_alive import keep_alive

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID")  # optional lock-down
CHECK_INTERVAL_MINUTES = int(os.environ.get("CHECK_INTERVAL_MINUTES", "15"))
ALARM_REPEAT_SECONDS = int(os.environ.get("ALARM_REPEAT_SECONDS", "60"))

# tracks which product_ids currently have a "keep pinging" alarm running,
# so /ack can cancel the asyncio task.
_active_alarms: dict[int, asyncio.Task] = {}


def _guard(chat_id) -> bool:
    if ALLOWED_CHAT_ID and str(chat_id) != str(ALLOWED_CHAT_ID):
        return False
    return True


def _bajaj_txt() -> str:
    # Bajaj EMI can't be reliably auto-detected (Flipkart loads it via
    # client-side JS, not static HTML) — always prompt a manual check
    # rather than show a possibly-false ✅/❌.
    return "❓ check manually on page"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Flipkart Price Tracker\n\n"
        "/add <flipkart_url> <target_price>\n"
        "/list\n"
        "/remove <id>\n"
        "/check <id> — check right now\n"
        "/ack <id> — silence an alarm once your target price hits"
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _guard(chat_id):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /add <flipkart_url> <target_price>")
        return

    url = context.args[0]
    try:
        target_price = float(context.args[1].replace(",", ""))
    except ValueError:
        await update.message.reply_text("Target price must be a number, e.g. 55000")
        return

    if "flipkart.com" not in url:
        await update.message.reply_text("That doesn't look like a Flipkart link.")
        return

    msg = await update.message.reply_text("Adding + fetching current price…")

    product_id = storage.add_product(chat_id, url, target_price)

    try:
        data = fetch_product(url)
        storage.update_price(product_id, data["price"], data["title"], data["bajaj_emi"])
        emi_txt = _bajaj_txt()
        await msg.edit_text(
            f"Tracking added (id {product_id})\n"
            f"📦 {data['title']}\n"
            f"💵 <b>₹{data['price']:,.0f}</b>\n"
            f"🎯 Target: ₹{target_price:,.0f}\n"
            f"🏦 Bajaj EMI: {emi_txt}",
            parse_mode="HTML",
        )
    except ScrapeError as e:
        await msg.edit_text(
            f"Added (id {product_id}) but couldn't fetch the price yet: {e}\n"
            f"It'll retry automatically every {CHECK_INTERVAL_MINUTES} min."
        )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _guard(chat_id):
        return
    products = storage.list_products(chat_id)
    if not products:
        await update.message.reply_text("Nothing tracked yet. Use /add <url> <target_price>.")
        return
    lines = []
    for p in products:
        price = f"<b>₹{p['last_price']:,.0f}</b>" if p["last_price"] else "not yet fetched"
        emi = _bajaj_txt()
        lines.append(
            f"#{p['id']} — {p['title'] or p['url'][:40]}\n"
            f"   price: {price} | target: ₹{p['target_price']:,.0f} | Bajaj EMI: {emi}"
        )
    await update.message.reply_text("\n\n".join(lines), parse_mode="HTML")


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _guard(chat_id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /remove <id>")
        return
    pid = int(context.args[0])
    ok = storage.remove_product(pid, chat_id)
    _cancel_alarm(pid)
    await update.message.reply_text("Removed." if ok else "No such product id.")


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _guard(chat_id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /check <id>")
        return
    pid = int(context.args[0])
    product = storage.get_product(pid)
    if not product or product["chat_id"] != chat_id:
        await update.message.reply_text("No such product id.")
        return
    try:
        fetch_product(product["url"])  # quick validation before the real check
    except ScrapeError as e:
        await update.message.reply_text(f"❌ Fetch failed: {e}")
        return
    await _check_one(context.application, product, force_notify=False)


async def cmd_ack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _guard(chat_id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /ack <id>")
        return
    pid = int(context.args[0])
    _cancel_alarm(pid)
    storage.set_alert_active(pid, False)
    await update.message.reply_text(f"Alarm for #{pid} silenced. 👍")


def _cancel_alarm(product_id):
    task = _active_alarms.pop(product_id, None)
    if task:
        task.cancel()


async def _repeating_alarm(app: Application, product: dict):
    """
    Escalating alarm until /ack'd — designed to have a real shot at
    waking someone up, within what's possible for free:
      - First 5 min: one ping every ALARM_REPEAT_SECONDS (default 60s)
      - After that: fires in bursts of 3 rapid messages (each is its
        own notification + sound) every 30s instead of a single ping —
        multiple back-to-back notification sounds are more likely to
        break through a silenced/Do Not Disturb phone than one message.
    Telegram's own notification settings still matter here — make sure
    this bot's notifications aren't muted and (ideally) that Telegram
    is allowed to override Do Not Disturb / silent mode in your
    phone's app-level notification settings.
    """
    chat_id = product["chat_id"]
    pid = product["id"]
    cycle = 0
    try:
        while True:
            escalated = cycle >= 5  # ~5 min of normal pings before escalating
            base_text = (
                f"🔴🔴🔴 PRICE TARGET HIT — #{pid}\n"
                f"{product['title']}\n"
                f"💵 <b>₹{product['last_price']:,.0f}</b> (target ₹{product['target_price']:,.0f})\n"
                f"Reply /ack {pid} to stop these pings."
            )
            if escalated:
                for _ in range(3):
                    await app.bot.send_message(chat_id=chat_id, text="🚨 WAKE UP — " + base_text, parse_mode="HTML")
                    await asyncio.sleep(2)
                await asyncio.sleep(30)
            else:
                await app.bot.send_message(chat_id=chat_id, text=base_text, parse_mode="HTML")
                await asyncio.sleep(ALARM_REPEAT_SECONDS)
            cycle += 1
    except asyncio.CancelledError:
        pass


async def _check_one(app: Application, product: dict, force_notify=False):
    pid = product["id"]
    try:
        data = fetch_product(product["url"])
    except ScrapeError as e:
        log.warning("Scrape failed for #%s: %s", pid, e)
        return

    old_price = product["last_price"]

    storage.update_price(pid, data["price"], data["title"], data["bajaj_emi"])

    title = html.escape(data["title"])
    price_line = f"💵 <b>₹{data['price']:,.0f}</b>"

    # Always notify — this is a heartbeat every check (default every 15
    # min), not just on change. A price drop gets a special highlight
    # since that's the one you actually care about acting on. Price is
    # bolded on its own line so it's the first thing you see, not
    # buried mid-sentence.
    if old_price is None:
        text = f"{title}\n{price_line}"
    elif data["price"] < old_price:
        drop = old_price - data["price"]
        text = (
            f"🟢⬇️ <b>PRICE DROPPED</b> — #{pid}\n"
            f"{title}\n"
            f"{price_line}\n"
            f"was ₹{old_price:,.0f} (down ₹{drop:,.0f})"
        )
    elif data["price"] > old_price:
        rise = data["price"] - old_price
        text = (
            f"🔺 Price update — #{pid}\n"
            f"{title}\n"
            f"{price_line}\n"
            f"was ₹{old_price:,.0f} (up ₹{rise:,.0f})"
        )
    else:
        text = (
            f"➖ No change — #{pid}\n"
            f"{title}\n"
            f"{price_line}\n"
            f"target ₹{product['target_price']:,.0f}"
        )
    text += f"\n🏦 Bajaj EMI: {_bajaj_txt()}"
    await app.bot.send_message(chat_id=product["chat_id"], text=text, parse_mode="HTML")

    # Target price hit -> start/continue the repeating alarm
    if data["price"] <= product["target_price"]:
        if pid not in _active_alarms:
            storage.set_alert_active(pid, True)
            product["last_price"] = data["price"]
            product["title"] = data["title"]
            task = asyncio.create_task(_repeating_alarm(app, product))
            _active_alarms[pid] = task
    else:
        _cancel_alarm(pid)
        storage.set_alert_active(pid, False)


async def scheduled_check(app: Application):
    for product in storage.get_all_products():
        await _check_one(app, product)


async def _post_init(app: Application):
    # Started here (inside run_polling's own event loop) rather than in
    # main() before the loop exists — on newer Python (3.12+, confirmed
    # on Render's Python 3.14) asyncio no longer auto-creates an event
    # loop for AsyncIOScheduler to attach to if called too early, which
    # crashes with "There is no current event loop in thread 'MainThread'".
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scheduled_check,
        "interval",
        minutes=CHECK_INTERVAL_MINUTES,
        args=[app],
    )
    scheduler.start()
    log.info("Scheduler started, checking every %s minutes", CHECK_INTERVAL_MINUTES)


def main():
    if not BOT_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN env var before running.")

    storage.init_db()
    keep_alive()

    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("ack", cmd_ack))

    log.info("Bot starting…")
    app.run_polling()


if __name__ == "__main__":
    main()
