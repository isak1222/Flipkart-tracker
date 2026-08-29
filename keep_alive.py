"""
Render's FREE tier only runs 'Web Service' type apps, which must bind
to a port and respond to HTTP requests (it pings itself to avoid
spinning down). This tiny Flask app satisfies that requirement while
the actual Telegram bot runs in a background thread.
"""
import threading
from flask import Flask

app = Flask(__name__)


@app.route("/")
def home():
    return "Flipkart tracker bot is running."


def run():
    app.run(host="0.0.0.0", port=8080)


def keep_alive():
    t = threading.Thread(target=run)
    t.daemon = True
    t.start()
