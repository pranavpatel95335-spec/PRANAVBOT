import os
import telebot
from flask import Flask, request

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
print(f"TOKEN LOADED: {TOKEN[:5]}...") # log me sirf 5 digit dikhega

bot = telebot.TeleBot(TOKEN, threaded=False)
app = Flask(__name__)

@bot.message_handler(commands=['start'])
def start(m):
    print(f"/start from {m.from_user.id}")
    bot.send_message(m.chat.id, "Bhai bot ON hai ✅ Finally reply aa gaya! Ab main pura code add karunga.")

@bot.message_handler(func=lambda m: True)
def echo(m):
    bot.send_message(m.chat.id, f"Tu bola: {m.text}")

@app.route('/')
def home():
    return "Bot LIVE hai!"

@app.route('/webhook', methods=['POST'])
def webhook():
    print("Webhook hit hua!")
    json_str = request.get_data().decode('UTF-8')
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "ok", 200

@app.route('/setwebhook')
def setwebhook():
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        return "RENDER_EXTERNAL_URL set karo"
    full = f"{url.rstrip('/')}/webhook"
    bot.remove_webhook()
    bot.set_webhook(url=full)
    return f"Webhook SET: {full} - Ab Telegram pe /start bhejo"
