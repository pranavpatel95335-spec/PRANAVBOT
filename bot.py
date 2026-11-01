import os
import random
import sqlite3
import requests
import telebot
import google.generativeai as genai
from flask import Flask, request

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL")
ADMIN_TELEGRAM_ID = int(os.environ.get("ADMIN_ID", 0))

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)

ai_model = genai.GenerativeModel(
    model_name='gemini-3.8-flash',
    system_instruction="You are a professional, polite, and advanced AI assistant built for public commercial use. You understand all languages (Hindi, English, Hinglish, etc.) and provide accurate, structured, and helpful responses to users with a premium corporate tone."
)

app = Flask(__name__)

def init_db():
    conn = sqlite3.connect("bot_users.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            email TEXT,
            otp TEXT,
            is_verified INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def send_otp_email(receiver_email, otp_code):
    try:
        payload = {
            "email": receiver_email,
            "otp": otp_code
        }
        response = requests.post(APPS_SCRIPT_URL, json=payload)
        result = response.json()
        return result.get("status") == "success"
    except Exception as e:
        print(f"Email Error: {e}")
        return False

@bot.message_handler(commands=['start'])
def start_command(message):
    user_id = message.from_user.id
    
    conn = sqlite3.connect("bot_users.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT is_verified FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    if row and row[0] == 1:
        if user_id == ADMIN_TELEGRAM_ID:
            bot.send_message(message.chat.id, "👑 **Welcome Boss (Pranav Ji)!** Your VIP AI platform is fully active. You can chat in any language or type `/admin` for the control panel.")
        else:
            bot.send_message(message.chat.id, "🌟 **Welcome to VIP AI Services!** Your account is verified. You can now chat in any language.")
    else:
        bot.send_message(message.chat.id, "🔐 **VIP Security Verification Required**\n\nPlease enter your official email address (e.g., `yourmail@gmail.com`) to receive your secure verification code.")
        bot.register_next_step_handler(message, process_email)

def process_email(message):
    user_id = message.from_user.id
    username = message.from_user.username or "No Username"
    email = message.text.strip()
    
    if "@" not in email or "." not in email:
        bot.send_message(message.chat.id, "⚠️ Invalid email format. Please enter a valid email address (e.g., `name@gmail.com`):")
        bot.register_next_step_handler(message, process_email)
        return
    
    otp = str(random.randint(100000, 999999))
    
    conn = sqlite3.connect("bot_users.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("REPLACE INTO users (user_id, username, email, otp, is_verified) VALUES (?, ?, ?, ?, 0)", 
                   (user_id, username, email, otp))
    conn.commit()
    conn.close()
    
    bot.send_message(message.chat.id, "📩 Generating and sending your secure verification code...")
    if send_otp_email(email, otp):
        bot.send_message(message.chat.id, f"🔐 A verification code has been successfully dispatched to `{email}`. Please enter the 6-digit code below:")
        bot.register_next_step_handler(message, process_otp)
    else:
        bot.send_message(message.chat.id, "❌ Failed to send email. Please enter your email address again:")
        bot.register_next_step_handler(message, process_email)

def process_otp(message):
    user_id = message.from_user.id
    user_otp = message.text.strip()
    
    conn = sqlite3.connect("bot_users.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT otp FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    
    if row and row[0] == user_otp:
        cursor.execute("UPDATE users SET is_verified = 1 WHERE user_id = ?", (user_id,))
        conn.commit()
        bot.send_message(message.chat.id, "🎉 **Verification Successful!** You now have full access to the VIP AI Platform. Send any message or prompt to begin.")
    else:
        bot.send_message(message.chat.id, "❌ Invalid code. Please enter the correct verification code:")
        bot.register_next_step_handler(message, process_otp)
    conn.close()

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if message.from_user.id != ADMIN_TELEGRAM_ID:
        bot.send_message(message.chat.id, "❌ Access Denied. This command is restricted to the platform owner.")
        return
    
    conn = sqlite3.connect("bot_users.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*), SUM(is_verified) FROM users")
    total, verified = cursor.fetchone()
    
    cursor.execute("SELECT user_id, username, email, is_verified FROM users")
    all_users = cursor.fetchall()
    conn.close()
    
    report = f"👑 **VIP Admin Control Center** 📊\n\n- Total Users: {total or 0} 👥\n- Verified VIPs: {verified or 0} ⭐\n\n📋 **User Directory:**\n"
    for u in all_users:
        status_text = "✅ VIP" if u[3] == 1 else "❌ Unverified"
        report += f"ID: `{u[0]}` | @{u[1]} | {u[2]} | {status_text}\n"
    
    bot.send_message(message.chat.id, report, parse_mode="Markdown")

@bot.message_handler(content_types=['text', 'photo', 'video', 'document'], func=lambda message: True)
def handle_ai_chat(message):
    user_id = message.from_user.id
    
    conn = sqlite3.connect("bot_users.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT is_verified FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row or row[0] != 1:
        bot.send_message(message.chat.id, "🔒 **Restricted Access:** Please complete your email verification by sending `/start` first.")
        return
    
    try:
        if message.text:
            response = ai_model.generate_content(message.text)
            reply_text = response.text
            bot.send_message(message.chat.id, f"{reply_text}\n\n*— VIP AI Assistant*")
        elif message.photo or message.video:
            bot.send_message(message.chat.id, "✅ Media received successfully. Processing AI analysis...")
    except Exception as e:
        bot.send_message(message.chat.id, f"⚠️ Error occurred: {e}")

@app.route('/')
def home():
    return "VIP AI Bot is running live successfully!"

# Telegram Webhook Route for Render
@app.route(f'/{TELEGRAM_BOT_TOKEN}', methods=['POST'])
def webhook():
    json_str = request.get_data().decode('UTF-8')
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "!", 200

if __name__ == "__main__":
    # Remove old webhook and set new webhook using Render URL
    bot.remove_webhook()
    # Render automatically provides your public URL or you can set it. Let's bind webhook:
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if render_url:
        bot.set_webhook(url=f"{render_url}/{TELEGRAM_BOT_TOKEN}")
    
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
