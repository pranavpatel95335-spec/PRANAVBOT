import os
import random
import sqlite3
import requests
import telebot
import google.generativeai as genai

# क्रेडेंशियल्स (Render के Environment Variables से)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
APPS_SCRIPT_URL = os.getenv("APPS_SCRIPT_URL")  # गूगल स्क्रिप्ट वाला वेब ऐप लिंक

# तेरी Telegram Admin ID
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))

# बॉट और जेमिनी इनिशियलाइज करें
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)

# जेमिनी मॉडल सेटअप (कोडिंग, टेक्स्ट और मल्टीमीडिया एनालिसिस के लिए)
ai_model = genai.GenerativeModel('gemini-1.5-flash')

# --- डेटाबेस सेटअप (VIP Users Table) ---
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

# --- Google Apps Script के जरिए जीमेल ओटीपी भेजने का फंक्शन ---
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

# --- VIP /start कमांड (प्रीमियम इमोजी के साथ) ---
@bot.message_handler(commands=['start'])
def start_command(message):
    user_id = message.from_user.id
    
    conn = sqlite3.connect("bot_users.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT is_verified FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    if row and row[0] == 1:
        bot.reply_to(message, "👑 **प्रणव जी के अल्टीमेट वीआईपी एआई बॉट में आपका स्वागत है, बॉस!** ⭐\n\nआपके सारे प्रीमियम फीचर्स पूरी तरह से अनलॉक हैं 🚀:\n- 💬 **स्मार्ट टेक्स्ट & कोडिंग चैट:** किसी भी भाषा में कोड, स्क्रिप्ट या सवाल पूछिए 🔥\n- 🎨 **VIP AI इमेज जनरेशन:** `/image <प्रॉम्प्ट>` लिखकर एचडी तस्वीरें बनाइए 💎\n- 🎬 **मीडिया/वीडियो एनालिसिस:** फोटो या वीडियो भेजिए, एआई उसका विश्लेषण करेगा ⚡")
    else:
        bot.reply_to(message, "🔒 **VIP SECURITY ACCESS RESTRICTED** 🛡️\n\nअरे भाई! यह एक प्रीमियम और सुरक्षित एआई बॉट है ⭐। इसे इस्तेमाल करने के लिए पहले अपनी सही ईमेल आईडी भेज (जैसे: `yourmail@gmail.com`) 📩, ताकि हम तेरी बिज़नेस आईडी से वीआईपी ओटीपी भेज सकें 🚀।")
        bot.register_next_step_handler(message, process_email)

def process_email(message):
    user_id = message.from_user.id
    username = message.from_user.username or "No Username"
    email = message.text.strip()
    
    if "@" not in email or "." not in email:
        bot.reply_to(message, "⚠️ भाई, सही ईमेल आईडी लिख कर भेज (जैसे: `name@gmail.com`) ❌। फिर से ईमेल भेज:")
        bot.register_next_step_handler(message, process_email)
        return
    
    otp = str(random.randint(100000, 999999))
    
    conn = sqlite3.connect("bot_users.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("REPLACE INTO users (user_id, username, email, otp, is_verified) VALUES (?, ?, ?, ?, 0)", 
                   (user_id, username, email, otp))
    conn.commit()
    conn.close()
    
    bot.reply_to(message, "📩 वीआईपी ओटीपी भेजा जा रहा है, थोड़ा इंतज़ार कर भाई... ⏳")
    if send_otp_email(email, otp):
        bot.reply_to(message, f"🔐 तेरी ईमेल ({email}) पर 6 अंकों का वीआईपी ओटीपी भेज दिया गया है ⭐। अपनी मेल चेक कर और यहाँ ओटीपी दर्ज कर 💎:")
        bot.register_next_step_handler(message, process_otp)
    else:
        bot.reply_to(message, "❌ ईमेल भेजने में दिक्कत आई भाई ⚠️। अपनी जीमेल आईडी दोबारा सही से भेज:")
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
        bot.reply_to(message, "🎉 **बधाई हो भाई! वीआईपी वेरिफिकेशन सफल रहा।** 🌟\n\nअब तू इस अल्टीमेट एआई बॉट का पूरा मालिक है 👑।\n- कोडिंग या चैट के लिए सीधा मैसेज कर 💻!\n- एचडी इमेज के लिए `/image <प्रॉम्प्ट>` लिख 🎨!")
    else:
        bot.reply_to(message, "❌ गलत ओटीपी है भाई! सही वीआईपी ओटीपी दोबारा डाल 🔄:")
        bot.register_next_step_handler(message, process_otp)
    conn.close()

# --- VIP इमेज जनरेशन कमांड (/image) ---
@bot.message_handler(commands=['image'])
def generate_image_command(message):
    user_id = message.from_user.id
    
    conn = sqlite3.connect("bot_users.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT is_verified FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row or row[0] != 1:
        bot.reply_to(message, "⛔ **एक्सेस Denied!** पहले भाई `/start` दबाकर अपनी ईमेल वेरीफाई कर 🔒, तभी वीआईपी इमेज जनरेट कर पाएगा ⭐!")
        return
    
    prompt = message.text.replace("/image", "").strip()
    if not prompt:
        bot.reply_to(message, "⚠️ भाई, इमेज का नाम या प्रॉम्प्ट भी तो लिख ✍️! जैसे: `/image a futuristic cyber city, 4k resolution` 🏙️")
        return
    
    bot.reply_to(message, "🎨 **VIP AI Image Engine** एक्टिव है 🚀... एचडी फोटो बनाई जा रही है, थोड़ा सब्र कर भाई ⏳...")
    
    try:
        image_model = genai.GenerativeModel('imagen-3.0-generate-002')
        result = image_model.generate_content(prompt)
        
        if hasattr(result, 'generated_images') and result.generated_images:
            for img in result.generated_images:
                bot.send_photo(message.chat.id, img.image.image_bytes, caption=f"✨ **VIP AI Masterpiece** 💎\nप्रॉम्प्ट: `{prompt}` 🎨")
        else:
            bot.reply_to(message, "भाई, इमेज जनरेट करने में कुछ दिक्कत आ गई ❌। दूसरा प्रॉम्प्ट ट्राई कर 🔄.")
    except Exception as e:
        bot.reply_to(message, f"इमेज बनाने में एरर आ गया भाई ⚠️: {e}")

# --- एडमिन पैनल कमांड (/admin) ---
@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if message.from_user.id != ADMIN_TELEGRAM_ID:
        bot.reply_to(message, "चलहट! तू एडमिन नहीं है भाई ❌। यह कमांड सिर्फ प्रणव जी (बॉस) के लिए है 👑।")
        return
    
    conn = sqlite3.connect("bot_users.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*), SUM(is_verified) FROM users")
    total, verified = cursor.fetchone()
    
    cursor.execute("SELECT user_id, username, email, is_verified FROM users")
    all_users = cursor.fetchall()
    conn.close()
    
    report = f"👑 **Admin Panel - Ultimate VIP Bot Control** 📊\n\n- कुल रजिस्टर्ड यूजर: {total or 0} 👥\n- वेरीफाइड (VIP) यूजर: {verified or 0} ⭐\n\n📋 **यूजर लिस्ट:**\n"
    for u in all_users:
        status_text = "✅ VIP" if u[3] == 1 else "❌ Unverified"
        report += f"ID: `{u[0]}` | @{u[1]} | {u[2]} | {status_text}\n"
    
    bot.reply_to(message, report, parse_mode="Markdown")

# --- अल्टीमेट एआई चैट, कोडिंग और मीडिया हैंडलर (सिर्फ वेरीफाइड यूजर्स के लिए) ---
@bot.message_handler(content_types=['text', 'photo', 'video', 'document'], func=lambda message: True)
def handle_ai_multimedia(message):
    user_id = message.from_user.id
    
    conn = sqlite3.connect("bot_users.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("SELECT is_verified FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row or row[0] != 1:
        bot.reply_to(message, "🔒 **लॉक्ड फीचर!** बिना ईमेल ओटीपी वेरिफिकेशन के तू एआई से बात या कोडिंग नहीं कर सकता ⛔। पहले `/start` दबाकर अपना अकाउंट वेरीफाई कर भाई ⭐!")
        return
    
    try:
        # यदि यूजर ने टेक्स्ट या कोडिंग का सवाल भेजा है
        if message.text:
            prompt_text = message.text
            response = ai_model.generate_content(prompt_text)
            bot.reply_to(message, f"{response.text}\n\n⭐ *Powered by Pranav AI* 🚀")
        
        # यदि यूजर ने फोटो या वीडियो भेजा है (मल्टीमीडिया एनालिसिस)
        elif message.photo or message.video:
            bot.reply_to(message, "⚡ मीडिया फाइल प्रोसेस की जा रही है, एआई विश्लेषण कर रहा है 🔍...")
            # यहाँ फाइल डाउनलोड करके जेमिनी विजन मॉडल को पास किया जा सकता है
            bot.reply_to(message, "✅ मीडिया रिसीव हो गया भाई! जेमिनी इस पर जल्द ही काम करेगा 🎬💎.")
            
    except Exception as e:
        bot.reply_to(message, f"भाई, एआई से कनेक्ट करने में दिक्कत आ रही है ⚠️: {e}")

print("प्रणव जी का अल्टीमेट वीआईपी एआई, कोडिंग और ओटीपी बॉट पूरी तरह से चालू हो गया है भाई... 🚀🔥")
bot.polling()
