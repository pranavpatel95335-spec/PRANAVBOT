import os, random, sqlite3, requests, telebot
import google.generativeai as genai
from flask import Flask, request

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL")
ADMIN_TELEGRAM_ID = int(os.environ.get("ADMIN_ID", 0))

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)

# YAHAN FIX KIYA HAI - sahi model name
ai_model = genai.GenerativeModel(
    model_name='gemini-1.5-flash',
    system_instruction="You are a professional, polite AI assistant..."
)

app = Flask(__name__)

def init_db():
    conn = sqlite3.connect("bot_users.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, email TEXT, otp TEXT, is_verified INTEGER DEFAULT 0)''')
    conn.commit()
    conn.close()
init_db()

def send_otp_email(receiver_email, otp_code):
    try:
        r = requests.post(APPS_SCRIPT_URL, json={"email": receiver_email, "otp": otp_code}, timeout=10)
        return r.json().get("status") == "success"
    except Exception as e:
        print(f"Email Error: {e}")
        return False

# ... tera start, process_email, process_otp, admin wala code same rahega ...

@app.route('/')
def home(): return "VIP AI Bot is running!"

@app.route(f'/{TELEGRAM_BOT_TOKEN}', methods=['POST'])
def webhook():
    json_str = request.get_data().decode('UTF-8')
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "!", 200

# Webhook set karne ke liye ek alag link
@app.route('/setwebhook')
def setwebhook():
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not render_url: return "RENDER_EXTERNAL_URL set nahi hai"
    bot.remove_webhook()
    bot.set_webhook(url=f"{render_url}/{TELEGRAM_BOT_TOKEN}")
    return f"Webhook set ho gaya: {render_url}/{TELEGRAM_BOT_TOKEN}"

@app.route(f'/<token>', methods=['POST']) # fallback nahi chahiye isliye upar wala hi use hoga
def dummy(token): return "!", 200
