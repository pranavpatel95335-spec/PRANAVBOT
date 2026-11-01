
import os
import re
import time
import hmac
import html
import json
import uuid
import hashlib
import secrets
import logging
import threading
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps

import requests
from flask import (
    Flask, request, session, redirect, url_for,
    render_template_string, abort
)
from waitress import serve

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

# =========================================================
# CONFIGURATION
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PANEL_PASSWORD", "")
SECRET_KEY = os.getenv("SECRET_KEY", "")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "")

BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "")
SENDER_NAME = os.getenv("SENDER_NAME", "SUPER AI BOT")

DATABASE = os.getenv("DATABASE_PATH", "super_ai_bot.db")
PORT = int(os.getenv("PORT", "10000"))

if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    logging.warning("SECRET_KEY missing. Sessions will reset after restart.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("SUPER_AI_BOT")

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12)
)

telegram_app = None
telegram_ready = False
telegram_start_error = None

# =========================================================
# DATABASE
# =========================================================

def db():
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            email TEXT DEFAULT '',
            email_verified INTEGER DEFAULT 0,
            banned INTEGER DEFAULT 0,
            vip_until INTEGER DEFAULT 0,
            joined_at INTEGER NOT NULL,
            ai_count INTEGER DEFAULT 0,
            image_count INTEGER DEFAULT 0,
            link_code TEXT DEFAULT '',
            link_expires INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS web_users (
            email TEXT PRIMARY KEY,
            verified INTEGER DEFAULT 0,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS otps (
            email TEXT NOT NULL,
            purpose TEXT NOT NULL,
            otp_hash TEXT NOT NULL,
            expires INTEGER NOT NULL,
            attempts INTEGER DEFAULT 0,
            last_sent INTEGER NOT NULL,
            PRIMARY KEY(email, purpose)
        );

        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS licenses (
            license_key TEXT PRIMARY KEY,
            days INTEGER NOT NULL,
            used_by INTEGER DEFAULT NULL,
            created_at INTEGER NOT NULL,
            used_at INTEGER DEFAULT NULL
        );

        CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT DEFAULT '',
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS feature_flags (
            feature TEXT PRIMARY KEY,
            enabled INTEGER DEFAULT 1
        );

        CREATE INDEX IF NOT EXISTS idx_chats_user
        ON chats(telegram_id, id);

        CREATE INDEX IF NOT EXISTS idx_audit_time
        ON audit(created_at);
        """)

        defaults = {
            "welcome": "Welcome to SUPER AI BOT V3 GOLDEN VIP EDITION!",
            "personality": "You are a helpful, clear and friendly AI assistant.",
            "daily_limit": "30",
            "vip_daily_limit": "300",
            "image_enabled": "0",
            "ai_enabled": "1",
            "maintenance": "0",
            "menu_title": "🌟 SUPER AI BOT V3",
        }

        for key, value in defaults.items():
            con.execute(
                "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                (key, value)
            )

        for feature in ("ai", "image", "profile", "vip", "history"):
            con.execute(
                "INSERT OR IGNORE INTO feature_flags(feature,enabled) VALUES(?,1)",
                (feature,)
            )


def setting(key, default=""):
    with db() as con:
        row = con.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with db() as con:
        con.execute("""
            INSERT INTO settings(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, str(value)))


def audit(actor, action, detail=""):
    with db() as con:
        con.execute("""
            INSERT INTO audit(actor,action,detail,created_at)
            VALUES(?,?,?,?)
        """, (str(actor), action, str(detail)[:1000], int(time.time())))


def feature_enabled(name):
    with db() as con:
        row = con.execute(
            "SELECT enabled FROM feature_flags WHERE feature=?", (name,)
        ).fetchone()
    return bool(row["enabled"]) if row else True


def set_feature(name, enabled):
    with db() as con:
        con.execute("""
            INSERT INTO feature_flags(feature,enabled) VALUES(?,?)
            ON CONFLICT(feature) DO UPDATE SET enabled=excluded.enabled
        """, (name, int(bool(enabled))))


def upsert_user(user):
    now = int(time.time())
    with db() as con:
        con.execute("""
            INSERT INTO users(
                telegram_id,username,first_name,joined_at
            ) VALUES(?,?,?,?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name
        """, (
            user.id,
            user.username or "",
            user.first_name or "",
            now
        ))


def get_user(tg_id):
    with db() as con:
        return con.execute(
            "SELECT * FROM users WHERE telegram_id=?", (tg_id,)
        ).fetchone()


def is_vip(tg_id):
    user = get_user(tg_id)
    return bool(user and int(user["vip_until"] or 0) > int(time.time()))


def access_allowed(tg_id):
    user = get_user(tg_id)
    if not user:
        return False, "Please send /start first."
    if user["banned"]:
        return False, "Your account is currently restricted."
    if setting("maintenance", "0") == "1" and str(tg_id) != ADMIN_ID:
        return False, "Bot is currently under maintenance."
    return True, ""


def daily_count(tg_id, role):
    start = int(datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp())

    with db() as con:
        row = con.execute("""
            SELECT COUNT(*) AS total FROM chats
            WHERE telegram_id=? AND role=? AND created_at>=?
        """, (tg_id, role, start)).fetchone()
    return int(row["total"])


def save_chat(tg_id, role, content):
    with db() as con:
        con.execute("""
            INSERT INTO chats(telegram_id,role,content,created_at)
            VALUES(?,?,?,?)
        """, (tg_id, role, content[:12000], int(time.time())))


def get_history(tg_id, limit=8):
    with db() as con:
        rows = con.execute("""
            SELECT role,content FROM chats
            WHERE telegram_id=?
            ORDER BY id DESC LIMIT ?
        """, (tg_id, limit)).fetchall()
    return list(reversed(rows))


# =========================================================
# EMAIL OTP - BREVO HTTPS API
# =========================================================

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def send_email(to_email, subject, body):
    if not BREVO_API_KEY or not SENDER_EMAIL:
        return False, "Email service is not configured."

    payload = {
        "sender": {
            "name": SENDER_NAME,
            "email": SENDER_EMAIL
        },
        "to": [{"email": to_email}],
        "subject": subject,
        "textContent": body
    }

    try:
        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "api-key": BREVO_API_KEY,
                "Content-Type": "application/json",
                "accept": "application/json"
            },
            json=payload,
            timeout=20
        )
        if response.status_code in (200, 201, 202):
            return True, "Sent"
        log.error("Brevo error: %s", response.text[:500])
        return False, "Email provider rejected the request."
    except requests.RequestException:
        log.exception("Email sending failed")
        return False, "Email provider is unavailable."


def otp_hash(email, purpose, otp):
    raw = f"{email.lower()}:{purpose}:{otp}:{SECRET_KEY}"
    return hashlib.sha256(raw.encode()).hexdigest()


def create_otp(email, purpose):
    email = email.lower().strip()

    if not EMAIL_RE.match(email):
        return False, "Enter a valid email address."

    now = int(time.time())

    with db() as con:
        old = con.execute("""
            SELECT last_sent FROM otps
            WHERE email=? AND purpose=?
        """, (email, purpose)).fetchone()

        if old and now - old["last_sent"] < 60:
            return False, "Please wait 60 seconds before requesting another OTP."

    otp = f"{secrets.randbelow(1000000):06d}"

    success, message = send_email(
        email,
        "Your SUPER AI BOT verification code",
        f"Your verification code is {otp}.\n"
        "It expires in 5 minutes.\n"
        "Do not share this code with anyone."
    )

    if not success:
        return False, message

    with db() as con:
        con.execute("""
            INSERT INTO otps(email,purpose,otp_hash,expires,attempts,last_sent)
            VALUES(?,?,?,?,0,?)
            ON CONFLICT(email,purpose) DO UPDATE SET
                otp_hash=excluded.otp_hash,
                expires=excluded.expires,
                attempts=0,
                last_sent=excluded.last_sent
        """, (
            email, purpose, otp_hash(email, purpose, otp),
            now + 300, now
        ))

    return True, "OTP sent."


def verify_otp(email, purpose, otp):
    email = email.lower().strip()
    now = int(time.time())

    with db() as con:
        row = con.execute("""
            SELECT * FROM otps WHERE email=? AND purpose=?
        """, (email, purpose)).fetchone()

        if not row:
            return False

        if row["expires"] < now or row["attempts"] >= 5:
            con.execute(
                "DELETE FROM otps WHERE email=? AND purpose=?",
                (email, purpose)
            )
            return False

        expected = otp_hash(email, purpose, otp.strip())

        if not hmac.compare_digest(row["otp_hash"], expected):
            con.execute("""
                UPDATE otps SET attempts=attempts+1
                WHERE email=? AND purpose=?
            """, (email, purpose))
            return False

        con.execute(
            "DELETE FROM otps WHERE email=? AND purpose=?",
            (email, purpose)
        )
        return True


# =========================================================
# GEMINI AI
# =========================================================

def gemini_reply(tg_id, user_message):
    if not GEMINI_API_KEY:
        return "AI service is not configured yet."

    user = get_user(tg_id)
    if not user or not user["email_verified"]:
        return "Please verify your email using /link before using AI."

    if setting("ai_enabled", "1") != "1" or not feature_enabled("ai"):
        return "AI chat is currently disabled."

    limit = int(
        setting("vip_daily_limit", "300")
        if is_vip(tg_id)
        else setting("daily_limit", "30")
    )

    if daily_count(tg_id, "user") >= limit:
        return "Your daily AI limit has been reached."

    history = get_history(tg_id, 8)

    contents = []
    for item in history:
        role = "model" if item["role"] == "assistant" else "user"
        contents.append({
            "role": role,
            "parts": [{"text": item["content"]}]
        })

    contents.append({
        "role": "user",
        "parts": [{"text": user_message[:8000]}]
    })

    payload = {
        "system_instruction": {
            "parts": [{"text": setting(
                "personality",
                "You are a helpful assistant."
            )}]
        },
        "contents": contents,
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 2048
        }
    }

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + GEMINI_MODEL + ":generateContent"
    )

    try:
        response = requests.post(
            url,
            headers={"x-goog-api-key": GEMINI_API_KEY},
            json=payload,
            timeout=60
        )

        if response.status_code != 200:
            log.error("Gemini error %s: %s",
                      response.status_code, response.text[:500])
            return "AI provider returned an error. Please try again later."

        data = response.json()
        parts = data.get("candidates", [{}])[0].get(
            "content", {}
        ).get("parts", [])

        answer = "\n".join(
            p.get("text", "") for p in parts if p.get("text")
        ).strip()

        if not answer:
            return "AI could not generate a text response."

        save_chat(tg_id, "user", user_message)
        save_chat(tg_id, "assistant", answer)

        with db() as con:
            con.execute("""
                UPDATE users SET ai_count=ai_count+1
                WHERE telegram_id=?
            """, (tg_id,))

        return answer[:12000]

    except requests.RequestException:
        log.exception("Gemini request failed")
        return "AI service is temporarily unavailable."


# =========================================================
# IMAGE GENERATION
# =========================================================

def generate_image(prompt):
    if not GEMINI_API_KEY or not GEMINI_IMAGE_MODEL:
        return None, "Image generation is not configured."

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + GEMINI_IMAGE_MODEL + ":generateContent"
    )

    payload = {
        "contents": [{
            "parts": [{"text": prompt[:2000]}]
        }],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"]
        }
    }

    try:
        response = requests.post(
            url,
            headers={"x-goog-api-key": GEMINI_API_KEY},
            json=payload,
            timeout=120
        )

        if response.status_code != 200:
            log.error("Image API error: %s", response.text[:500])
            return None, "The configured image model did not accept the request."

        data = response.json()
        parts = data.get("candidates", [{}])[0].get(
            "content", {}
        ).get("parts", [])

        for part in parts:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                import base64
                return base64.b64decode(inline["data"]), None

        return None, "The image model returned no image."

    except Exception:
        log.exception("Image generation failed")
        return None, "Image generation is temporarily unavailable."


# =========================================================
# TELEGRAM BOT
# =========================================================

def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🤖 AI CHAT", callback_data="ai"),
            InlineKeyboardButton("🎨 IMAGE STUDIO", callback_data="image")
        ],
        [
            InlineKeyboardButton("👤 MY PROFILE", callback_data="profile"),
            InlineKeyboardButton("👑 VIP MEMBERSHIP", callback_data="vip")
        ],
        [
            InlineKeyboardButton("📜 AI HISTORY", callback_data="history"),
            InlineKeyboardButton("⚙️ SETTINGS", callback_data="settings")
        ],
        [
            InlineKeyboardButton("❓ HELP", callback_data="help")
        ]
    ])


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user)

    text = (
        f"{setting('menu_title', '🌟 SUPER AI BOT V3')}\n\n"
        f"Hello {html.escape(user.first_name or 'User')}!\n\n"
        f"{setting('welcome')}\n\n"
        "Verify your email with /link to unlock AI features."
    )

    await update.message.reply_text(
        text,
        reply_markup=main_keyboard()
    )


async def link_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user)

    code = f"{secrets.randbelow(1000000):06d}"

    with db() as con:
        con.execute("""
            UPDATE users SET link_code=?,link_expires=?
            WHERE telegram_id=?
        """, (code, int(time.time()) + 600, user.id))

    await update.message.reply_text(
        "🔐 ACCOUNT LINKING\n\n"
        f"Your one-time linking code is: {code}\n\n"
        "1. Open your bot website.\n"
        "2. Verify your email using OTP.\n"
        "3. Enter this Telegram linking code.\n\n"
        "The code expires in 10 minutes. Do not share it."
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Current operation cancelled.")


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update.effective_user)
    await show_profile(update, update.effective_user.id)


async def vip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update.effective_user)
    await update.message.reply_text(
        "👑 VIP MEMBERSHIP\n\n"
        "VIP provides a higher daily AI usage limit.\n"
        "Contact the administrator for membership."
    )


async def image_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update.effective_user)

    allowed, message = access_allowed(update.effective_user.id)
    if not allowed:
        await update.message.reply_text(message)
        return

    if not feature_enabled("image") or setting("image_enabled", "0") != "1":
        await update.message.reply_text("Image Studio is currently disabled.")
        return

    if not get_user(update.effective_user.id)["email_verified"]:
        await update.message.reply_text("Verify your email first using /link.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: /image describe the image you want"
        )
        return

    limit = 20 if is_vip(update.effective_user.id) else 3
    if daily_count(update.effective_user.id, "image") >= limit:
        await update.message.reply_text("Your daily image limit is reached.")
        return

    await update.message.reply_text("🎨 Creating your image...")

    image_data, error = generate_image(" ".join(context.args))

    if error:
        await update.message.reply_text(error)
        return

    import io
    await update.message.reply_photo(
        photo=io.BytesIO(image_data),
        caption="✨ Generated by SUPER AI BOT V3"
    )

    save_chat(update.effective_user.id, "image", " ".join(context.args))

    with db() as con:
        con.execute("""
            UPDATE users SET image_count=image_count+1
            WHERE telegram_id=?
        """, (update.effective_user.id,))


async def show_profile(update, tg_id):
    user = get_user(tg_id)
    if not user:
        return

    vip_text = (
        "Active"
        if is_vip(tg_id)
        else "Standard"
    )

    email = user["email"] or "Not linked"
    verified = "Verified" if user["email_verified"] else "Not verified"

    text = (
        "👤 YOUR PROFILE\n\n"
        f"Telegram ID: {tg_id}\n"
        f"Username: @{user['username'] or 'Not set'}\n"
        f"Email: {email}\n"
        f"Email status: {verified}\n"
        f"Membership: {vip_text}\n"
        f"AI requests: {user['ai_count']}\n"
        f"Image requests: {user['image_count']}\n"
        f"Joined: {datetime.fromtimestamp(user['joined_at']).strftime('%d-%m-%Y')}"
    )

    if hasattr(update, "callback_query") and update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=main_keyboard()
        )
    else:
        await update.message.reply_text(text, reply_markup=main_keyboard())


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    tg_id = query.from_user.id
    upsert_user(query.from_user)

    if query.data == "profile":
        await show_profile(update, tg_id)

    elif query.data == "vip":
        await query.edit_message_text(
            "👑 VIP MEMBERSHIP\n\n"
            "VIP members receive a higher daily AI limit.\n"
            "Contact the administrator for membership.",
            reply_markup=main_keyboard()
        )

    elif query.data == "ai":
        context.user_data["mode"] = "ai"
        await query.edit_message_text(
            "🤖 AI CHAT MODE\n\nSend your message now.\n"
            "Use /cancel to leave this mode."
        )

    elif query.data == "image":
        context.user_data["mode"] = "image"
        await query.edit_message_text(
            "🎨 IMAGE STUDIO\n\n"
            "Describe the image you want.\n"
            "Use /cancel to leave this mode."
        )

    elif query.data == "history":
        rows = get_history(tg_id, 10)
        if not rows:
            text = "No chat history found."
        else:
            text = "📜 RECENT HISTORY\n\n"
            for row in rows[-10:]:
                text += f"{row['role'].upper()}: {row['content'][:250]}\n\n"

        await query.edit_message_text(
            text[:4000], reply_markup=main_keyboard()
        )

    elif query.data == "settings":
        await query.edit_message_text(
            "⚙️ SETTINGS\n\n"
            "Use /link to link your verified email.\n"
            "Use /cancel to cancel the current operation.",
            reply_markup=main_keyboard()
        )

    elif query.data == "help":
        await query.edit_message_text(
            "❓ HELP\n\n"
            "/start - Open the main menu\n"
            "/link - Link and verify your email\n"
            "/profile - View your profile\n"
            "/vip - Membership information\n"
            "/image - Generate an image (if enabled)\n"
            "/cancel - Cancel current operation",
            reply_markup=main_keyboard()
        )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user)

    allowed, message = access_allowed(user.id)
    if not allowed:
        await update.message.reply_text(message)
        return

    record = get_user(user.id)

    if not record["email_verified"]:
        await update.message.reply_text(
            "Please verify your email first using /link."
        )
        return

    mode = context.user_data.get("mode", "ai")

    if mode == "image":
        if not feature_enabled("image") or setting("image_enabled", "0") != "1":
            await update.message.reply_text("Image Studio is disabled.")
            return

        limit = 20 if is_vip(user.id) else 3
        if daily_count(user.id, "image") >= limit:
            await update.message.reply_text("Your daily image limit is reached.")
            return

        await update.message.chat.send_action(ChatAction.UPLOAD_PHOTO)
        image_data, error = generate_image(update.message.text)

        if error:
            await update.message.reply_text(error)
            return

        import io
        await update.message.reply_photo(
            photo=io.BytesIO(image_data),
            caption="✨ SUPER AI BOT V3"
        )
        save_chat(user.id, "image", update.message.text)

        with db() as con:
            con.execute("""
                UPDATE users SET image_count=image_count+1
                WHERE telegram_id=?
            """, (user.id,))
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    answer = gemini_reply(user.id, update.message.text)
    await update.message.reply_text(answer)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ADMIN_ID:
        await update.message.reply_text("Access denied.")
        return

    await update.message.reply_text(
        "ADMIN COMMANDS\n\n"
        "/stats - Bot statistics\n"
        "/ban USER_ID\n"
        "/unban USER_ID\n"
        "/vipadd USER_ID DAYS\n"
        "/broadcast MESSAGE"
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ADMIN_ID:
        return

    with db() as con:
        total = con.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        banned = con.execute(
            "SELECT COUNT(*) n FROM users WHERE banned=1"
        ).fetchone()["n"]
        verified = con.execute(
            "SELECT COUNT(*) n FROM users WHERE email_verified=1"
        ).fetchone()["n"]

    await update.message.reply_text(
        f"📊 BOT STATISTICS\n\nUsers: {total}\n"
        f"Verified: {verified}\nBanned: {banned}"
    )


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ADMIN_ID:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /ban USER_ID")
        return

    tg_id = int(context.args[0])
    with db() as con:
        con.execute("UPDATE users SET banned=1 WHERE telegram_id=?", (tg_id,))
    audit(ADMIN_ID, "ban", tg_id)
    await update.message.reply_text("User banned.")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ADMIN_ID:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /unban USER_ID")
        return

    tg_id = int(context.args[0])
    with db() as con:
        con.execute("UPDATE users SET banned=0 WHERE telegram_id=?", (tg_id,))
    audit(ADMIN_ID, "unban", tg_id)
    await update.message.reply_text("User unbanned.")


async def vipadd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ADMIN_ID:
        return

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /vipadd USER_ID DAYS")
        return

    try:
        tg_id = int(context.args[0])
        days = int(context.args[1])
        if days < 1 or days > 36500:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Enter a valid user ID and days.")
        return

    current = int(time.time())
    with db() as con:
        row = con.execute(
            "SELECT vip_until FROM users WHERE telegram_id=?", (tg_id,)
        ).fetchone()

        if not row:
            await update.message.reply_text("User not found.")
            return

        expiry = max(current, int(row["vip_until"] or 0)) + days * 86400
        con.execute(
            "UPDATE users SET vip_until=? WHERE telegram_id=?",
            (expiry, tg_id)
        )

    audit(ADMIN_ID, "vip_add", f"{tg_id}:{days}")
    await update.message.reply_text("VIP membership updated.")


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ADMIN_ID:
        return

    message = " ".join(context.args).strip()
    if not message:
        await update.message.reply_text("Usage: /broadcast MESSAGE")
        return

    with db() as con:
        users = con.execute(
            "SELECT telegram_id FROM users WHERE banned=0"
        ).fetchall()

    sent = 0
    failed = 0

    for row in users:
        try:
            await context.bot.send_message(
                chat_id=row["telegram_id"],
                text=message[:4000]
            )
            sent += 1
        except Exception:
            failed += 1

    audit(ADMIN_ID, "broadcast", f"sent={sent}, failed={failed}")
    await update.message.reply_text(
        f"Broadcast complete.\nSent: {sent}\nFailed: {failed}"
    )


async def post_init(application):
    global telegram_ready
    telegram_ready = True
    log.info("Telegram bot started.")


async def post_shutdown(application):
    global telegram_ready
    telegram_ready = False


def start_telegram():
    global telegram_app, telegram_start_error

    if not BOT_TOKEN:
        telegram_start_error = "BOT_TOKEN is missing."
        log.error(telegram_start_error)
        return

    try:
        telegram_app = (
            Application.builder()
            .token(BOT_TOKEN)
            .post_init(post_init)
            .post_shutdown(post_shutdown)
            .build()
        )

        telegram_app.add_handler(CommandHandler("start", start_command))
        telegram_app.add_handler(CommandHandler("link", link_command))
        telegram_app.add_handler(CommandHandler("cancel", cancel_command))
        telegram_app.add_handler(CommandHandler("profile", profile_command))
        telegram_app.add_handler(CommandHandler("vip", vip_command))
        telegram_app.add_handler(CommandHandler("image", image_command))
        telegram_app.add_handler(CommandHandler("admin", admin_command))
        telegram_app.add_handler(CommandHandler("stats", stats_command))
        telegram_app.add_handler(CommandHandler("ban", ban_command))
        telegram_app.add_handler(CommandHandler("unban", unban_command))
        telegram_app.add_handler(CommandHandler("vipadd", vipadd_command))
        telegram_app.add_handler(CommandHandler("broadcast", broadcast_command))

        telegram_app.add_handler(CallbackQueryHandler(button_handler))
        telegram_app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
        )

        telegram_app.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES
        )

    except Exception as exc:
        telegram_start_error = str(exc)
        log.exception("Telegram startup failed")


# =========================================================
# WEB HELPERS
# =========================================================

def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(24)
    return session["_csrf"]


def check_csrf():
    token = request.form.get("_csrf", "")
    expected = session.get("_csrf", "")
    if not expected or not hmac.compare_digest(token, expected):
        abort(403)


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapper


def web_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("email"):
            return redirect(url_for("web_login"))
        return fn(*args, **kwargs)
    return wrapper


BASE_STYLE = """
<style>
*{box-sizing:border-box}
body{margin:0;background:#080d1c;color:#eef2ff;
font-family:Arial,sans-serif;padding:20px}
.wrap{max-width:1050px;margin:auto}
.card{background:#111b32;border:1px solid #28395e;
border-radius:16px;padding:20px;margin:14px 0}
h1,h2{color:#f5c96b}
input,textarea,select{width:100%;padding:12px;margin:7px 0;
border-radius:9px;border:1px solid #435477;background:#091225;
color:white}
button,.btn{display:inline-block;padding:11px 16px;
background:#bd913d;color:#111;border:0;border-radius:9px;
font-weight:bold;text-decoration:none;cursor:pointer;margin:4px 0}
a{color:#f5cf78}
small{color:#9daecc}
table{width:100%;border-collapse:collapse}
td,th{padding:10px;border-bottom:1px solid #34415b;text-align:left}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
</style>
"""


def page(title, body):
    return render_template_string(
        "<!doctype html><html><head><meta name='viewport' "
        "content='width=device-width,initial-scale=1'>"
        "<title>{{title}}</title>" + BASE_STYLE +
        "</head><body><div class='wrap'>"
        "<h1>🌟 SUPER AI BOT V3</h1>{{body|safe}}"
        "</div></body></html>",
        title=title, body=body
    )


# =========================================================
# WEBSITE LOGIN + EMAIL VERIFICATION
# =========================================================

@app.route("/")
def home():
    status = "Online" if telegram_ready else "Telegram not connected"
    return page("Home", f"""
    <div class="card">
      <h2>GOLDEN VIP EDITION</h2>
      <p>AI Assistant • Image Studio • VIP Membership</p>
      <p>Telegram status: {html.escape(status)}</p>
      <a class="btn" href="/login">User Login</a>
      <a class="btn" href="/admin">Admin Panel</a>
    </div>
    <div class="card">
      <small>Account verification is required before AI access.</small>
    </div>
    """)


@app.route("/login", methods=["GET", "POST"])
def web_login():
    message = ""

    if request.method == "POST":
        check_csrf()
        email = request.form.get("email", "").lower().strip()
        action = request.form.get("action", "")

        if not EMAIL_RE.match(email):
            message = "Enter a valid email address."

        elif action == "send":
            ok, result = create_otp(email, "website")
            message = result

        elif action == "verify":
            otp = request.form.get("otp", "")
            if verify_otp(email, "website", otp):
                with db() as con:
                    con.execute("""
                        INSERT INTO web_users(email,verified,created_at)
                        VALUES(?,1,?)
                        ON CONFLICT(email) DO UPDATE SET verified=1
                    """, (email, int(time.time())))

                session.clear()
                session.permanent = True
                session["email"] = email
                return redirect(url_for("web_profile"))
            message = "Incorrect or expired OTP."

        elif action == "link":
            code = request.form.get("link_code", "").strip()

            if not re.fullmatch(r"\d{6}", code):
                message = "Enter the six-digit Telegram linking code."
            else:
                with db() as con:
                    target = con.execute("""
                        SELECT telegram_id FROM users
                        WHERE link_code=? AND link_expires>=?
                    """, (code, int(time.time()))).fetchone()

                    if not target:
                        message = "Invalid or expired Telegram code."
                    else:
                        con.execute("""
                            UPDATE users
                            SET email=?,email_verified=1,
                                link_code='',link_expires=0
                            WHERE telegram_id=?
                        """, (email, target["telegram_id"]))

                        audit(email, "telegram_link", target["telegram_id"])
                        message = "Telegram account successfully linked."

    token = csrf_token()
    safe_message = html.escape(message)

    body = f"""
    <div class="card">
      <h2>📧 Email Verification</h2>
      <p>{safe_message}</p>
      <form method="post">
        <input type="hidden" name="_csrf" value="{token}">
        <input name="email" type="email" placeholder="Your email address" required>
        <button name="action" value="send">Send OTP</button>
        <input name="otp" placeholder="6-digit OTP" maxlength="6">
        <button name="action" value="verify">Verify Email</button>
      </form>
    </div>
    <div class="card">
      <h2>🔗 Link Telegram Account</h2>
      <p>First verify your email above. Then send /link to the Telegram bot.</p>
      <form method="post">
        <input type="hidden" name="_csrf" value="{token}">
        <input name="email" type="email" placeholder="Verified email address" required>
        <input name="link_code" placeholder="Telegram 6-digit code" maxlength="6" required>
        <button name="action" value="link">Link Telegram</button>
      </form>
    </div>
    """

    return page("Login", body)


@app.route("/profile")
@web_required
def web_profile():
    email = session["email"]

    with db() as con:
        linked = con.execute(
            "SELECT * FROM users WHERE email=?", (email,)
        ).fetchone()

    if linked:
        profile = (
            f"Telegram ID: {linked['telegram_id']}<br>"
            f"Username: @{html.escape(linked['username'] or 'Not set')}<br>"
            f"AI requests: {linked['ai_count']}<br>"
            f"Image requests: {linked['image_count']}<br>"
            f"VIP: {'Active' if is_vip(linked['telegram_id']) else 'Standard'}"
        )
    else:
        profile = "No Telegram account linked yet. Send /link to the bot."

    return page("Profile", f"""
    <div class="card">
      <h2>👤 Your Account</h2>
      <p>{html.escape(email)}</p>
      <p>{profile}</p>
      <a class="btn" href="/logout">Logout</a>
    </div>
    """)


@app.route("/logout")
def web_logout():
    session.clear()
    return redirect(url_for("home"))


# =========================================================
# ADMIN PANEL
# =========================================================

@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    message = ""

    if request.method == "POST":
        check_csrf()
        password = request.form.get("password", "")

        if ADMIN_PASSWORD and hmac.compare_digest(
            password, ADMIN_PASSWORD
        ):
            session.clear()
            session["admin"] = True
            session.permanent = True
            audit("web_admin", "login")
            return redirect(url_for("admin_dashboard"))

        message = "Invalid admin password."

    token = csrf_token()

    return page("Admin Login", f"""
    <div class="card">
      <h2>🔐 Administrator Login</h2>
      <p>{html.escape(message)}</p>
      <form method="post">
        <input type="hidden" name="_csrf" value="{token}">
        <input type="password" name="password"
               placeholder="Admin password" required>
        <button>Login</button>
      </form>
    </div>
    """)


@app.route("/admin/logout")
@admin_required
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin/dashboard", methods=["GET", "POST"])
@admin_required
def admin_dashboard():
    message = ""

    if request.method == "POST":
        check_csrf()
        action = request.form.get("action", "")

        try:
            if action == "settings":
                allowed = (
                    "welcome", "personality", "menu_title",
                    "daily_limit", "vip_daily_limit",
                    "ai_enabled", "image_enabled", "maintenance"
                )

                for key in allowed:
                    if key in request.form:
                        value = request.form.get(key, "").strip()

                        if key in ("daily_limit", "vip_daily_limit"):
                            value = str(max(1, min(10000, int(value))))

                        if key in (
                            "ai_enabled", "image_enabled", "maintenance"
                        ):
                            value = "1" if value == "1" else "0"

                        set_setting(key, value)

                audit("web_admin", "settings_updated")
                message = "Settings saved."

            elif action == "user":
                tg_id = int(request.form["telegram_id"])
                operation = request.form["operation"]

                with db() as con:
                    if operation == "ban":
                        con.execute(
                            "UPDATE users SET banned=1 WHERE telegram_id=?",
                            (tg_id,)
                        )
                    elif operation == "unban":
                        con.execute(
                            "UPDATE users SET banned=0 WHERE telegram_id=?",
                            (tg_id,)
                        )
                    elif operation == "remove_vip":
                        con.execute(
                            "UPDATE users SET vip_until=0 WHERE telegram_id=?",
                            (tg_id,)
                        )

                audit("web_admin", operation, tg_id)
                message = "User updated."

            elif action == "vip":
                tg_id = int(request.form["telegram_id"])
                days = int(request.form["days"])

                if days < 1 or days > 36500:
                    raise ValueError("Days must be between 1 and 36500.")

                with db() as con:
                    row = con.execute(
                        "SELECT vip_until FROM users WHERE telegram_id=?",
                        (tg_id,)
                    ).fetchone()

                    if not row:
                        raise ValueError("User not found.")

                    expiry = max(
                        int(time.time()), int(row["vip_until"] or 0)
                    ) + days * 86400

                    con.execute(
                        "UPDATE users SET vip_until=? WHERE telegram_id=?",
                        (expiry, tg_id)
                    )

                audit("web_admin", "vip_added", f"{tg_id}:{days}")
                message = "VIP membership updated."

            elif action == "license":
                days = int(request.form["days"])
                quantity = int(request.form.get("quantity", "1"))

                if not 1 <= days <= 36500 or not 1 <= quantity <= 50:
                    raise ValueError("Invalid license quantity or duration.")

                created = []

                with db() as con:
                    for _ in range(quantity):
                        key = "GOLD-" + secrets.token_hex(8).upper()
                        con.execute("""
                            INSERT INTO licenses(
                                license_key,days,created_at
                            ) VALUES(?,?,?)
                        """, (key, days, int(time.time())))
                        created.append(key)

                audit("web_admin", "licenses_created", quantity)
                message = "Created keys: " + ", ".join(created)

            elif action == "feature":
                name = request.form["feature"]
                enabled = request.form.get("enabled") == "1"

                if name not in ("ai", "image", "profile", "vip", "history"):
                    raise ValueError("Unknown feature.")

                set_feature(name, enabled)
                audit("web_admin", "feature_changed", name)
                message = "Feature updated."

            elif action == "message":
                tg_id = int(request.form["telegram_id"])
                text = request.form["message"].strip()

                if not text or not telegram_app:
                    raise ValueError("Message empty or bot unavailable.")

                telegram_app.create_task(
                    telegram_app.bot.send_message(
                        chat_id=tg_id, text=text[:4000]
                    )
                )
                audit("web_admin", "individual_message", tg_id)
                message = "Message queued."

            elif action == "broadcast":
                text = request.form.get("message", "").strip()

                if not text or not telegram_app:
                    raise ValueError("Message empty or bot unavailable.")

                telegram_app.create_task(
                    broadcast_from_web(text)
                )
                audit("web_admin", "broadcast_queued")
                message = "Broadcast queued."

        except (ValueError, KeyError) as exc:
            message = str(exc)
        except Exception:
            log.exception("Admin action failed")
            message = "Action failed. Check server logs."

    with db() as con:
        total = con.execute(
            "SELECT COUNT(*) n FROM users"
        ).fetchone()["n"]
        verified = con.execute(
            "SELECT COUNT(*) n FROM users WHERE email_verified=1"
        ).fetchone()["n"]
        banned = con.execute(
            "SELECT COUNT(*) n FROM users WHERE banned=1"
        ).fetchone()["n"]
        users = con.execute("""
            SELECT telegram_id,username,email,email_verified,banned,vip_until
            FROM users ORDER BY joined_at DESC LIMIT 100
        """).fetchall()
        logs = con.execute("""
            SELECT actor,action,detail,created_at FROM audit
            ORDER BY id DESC LIMIT 20
        """).fetchall()
        licenses = con.execute("""
            SELECT license_key,days,used_by FROM licenses
            ORDER BY created_at DESC LIMIT 20
        """).fetchall()

    token = csrf_token()

    user_rows = ""
    for u in users:
        user_rows += (
            "<tr><td>" + str(u["telegram_id"]) + "</td>"
            "<td>" + html.escape(u["username"] or "-") + "</td>"
            "<td>" + html.escape(u["email"] or "-") + "</td>"
            "<td>" + ("Yes" if u["email_verified"] else "No") + "</td>"
            "<td>" + ("Banned" if u["banned"] else "Active") + "</td>"
            "</tr>"
        )

    log_rows = ""
    for item in logs:
        log_rows += (
            "<tr><td>" + html.escape(item["actor"]) + "</td>"
            "<td>" + html.escape(item["action"]) + "</td>"
            "<td>" + html.escape(item["detail"]) + "</td></tr>"
        )

    license_rows = ""
    for item in licenses:
        license_rows += (
            "<tr><td>" + html.escape(item["license_key"]) + "</td>"
            "<td>" + str(item["days"]) + "</td>"
            "<td>" + str(item["used_by"] or "Unused") + "</td></tr>"
        )

    body = f"""
    <div class="card">
      <h2>👑 ADMIN DASHBOARD</h2>
      <p>{html.escape(message)}</p>
      <div class="row">
        <div class="card">Total Users<br><h2>{total}</h2></div>
        <div class="card">Verified Users<br><h2>{verified}</h2></div>
        <div class="card">Banned Users<br><h2>{banned}</h2></div>
      </div>
      <a class="btn" href="/admin/logout">Logout</a>
    </div>

    <div class="card">
      <h2>⚙️ BOT SETTINGS</h2>
      <form method="post">
        <input type="hidden" name="_csrf" value="{token}">
        <input type="hidden" name="action" value="settings">
        <label>Menu title</label>
        <input name="menu_title" value="{html.escape(setting('menu_title'))}">
        <label>Welcome message</label>
        <textarea name="welcome">{html.escape(setting('welcome'))}</textarea>
        <label>AI personality</label>
        <textarea name="personality">{html.escape(setting('personality'))}</textarea>
        <label>Daily AI limit</label>
        <input type="number" name="daily_limit"
               value="{html.escape(setting('daily_limit'))}">
        <label>VIP daily AI limit</label>
        <input type="number" name="vip_daily_limit"
               value="{html.escape(setting('vip_daily_limit'))}">
        <label>AI enabled (1 or 0)</label>
        <input name="ai_enabled" value="{html.escape(setting('ai_enabled'))}">
        <label>Image enabled (1 or 0)</label>
        <input name="image_enabled" value="{html.escape(setting('image_enabled'))}">
        <label>Maintenance (1 or 0)</label>
        <input name="maintenance" value="{html.escape(setting('maintenance'))}">
        <button>Save Settings</button>
      </form>
    </div>

    <div class="card">
      <h2>🧩 FEATURE CONTROLS</h2>
      <form method="post">
        <input type="hidden" name="_csrf" value="{token}">
        <input type="hidden" name="action" value="feature">
        <select name="feature">
          <option value="ai">AI Chat</option>
          <option value="image">Image Studio</option>
          <option value="profile">Profile</option>
          <option value="vip">VIP</option>
          <option value="history">History</option>
        </select>
        <select name="enabled">
          <option value="1">Enable</option>
          <option value="0">Disable</option>
        </select>
        <button>Update Feature</button>
      </form>
    </div>

    <div class="card">
      <h2>👤 USER CONTROL</h2>
      <form method="post">
        <input type="hidden" name="_csrf" value="{token}">
        <input type="hidden" name="action" value="user">
        <input name="telegram_id" placeholder="Telegram User ID" required>
        <select name="operation">
          <option value="ban">Ban</option>
          <option value="unban">Unban</option>
          <option value="remove_vip">Remove VIP</option>
        </select>
        <button>Apply</button>
      </form>
    </div>

    <div class="card">
      <h2>👑 GIVE VIP</h2>
      <form method="post">
        <input type="hidden" name="_csrf" value="{token}">
        <input type="hidden" name="action" value="vip">
        <input name="telegram_id" placeholder="Telegram User ID" required>
        <input type="number" name="days" placeholder="Days" required>
        <button>Activate VIP</button>
      </form>
    </div>

    <div class="card">
      <h2>🔑 LICENSE GENERATOR</h2>
      <form method="post">
        <input type="hidden" name="_csrf" value="{token}">
        <input type="hidden" name="action" value="license">
        <input type="number" name="days" placeholder="Validity in days" required>
        <input type="number" name="quantity" placeholder="Quantity (1-50)" value="1">
        <button>Generate License Keys</button>
      </form>
      <table><tr><th>Key</th><th>Days</th><th>Used by</th></tr>
      {license_rows}</table>
    </div>

    <div class="card">
      <h2>📨 MESSAGE USER</h2>
      <form method="post">
        <input type="hidden" name="_csrf" value="{token}">
        <input type="hidden" name="action" value="message">
        <input name="telegram_id" placeholder="Telegram User ID" required>
        <textarea name="message" placeholder="Message" required></textarea>
        <button>Send Message</button>
      </form>
    </div>

    <div class="card">
      <h2>📢 BROADCAST</h2>
      <form method="post">
        <input type="hidden" name="_csrf" value="{token}">
        <input type="hidden" name="action" value="broadcast">
        <textarea name="message" placeholder="Broadcast message" required></textarea>
        <button>Broadcast to Users</button>
      </form>
    </div>

    <div class="card">
      <h2>👥 RECENT USERS</h2>
      <table>
        <tr><th>ID</th><th>Username</th><th>Email</th><th>Verified</th><th>Status</th></tr>
        {user_rows}
      </table>
    </div>

    <div class="card">
      <h2>🛡️ AUDIT LOG</h2>
      <table>
        <tr><th>Actor</th><th>Action</th><th>Details</th></tr>
        {log_rows}
      </table>
    </div>
    """

    return page("Admin Dashboard", body)


async def broadcast_from_web(message):
    if not telegram_app:
        return

    with db() as con:
        users = con.execute(
            "SELECT telegram_id FROM users WHERE banned=0"
        ).fetchall()

    for user in users:
        try:
            await telegram_app.bot.send_message(
                chat_id=user["telegram_id"],
                text=message[:4000]
            )
        except Exception:
            log.warning("Broadcast failed for %s", user["telegram_id"])


# =========================================================
# LICENSE REDEMPTION
# =========================================================

async def redeem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user)

    if not context.args:
        await update.message.reply_text("Usage: /redeem YOUR_LICENSE_KEY")
        return

    key = context.args[0].strip().upper()
    now = int(time.time())

    with db() as con:
        license_row = con.execute("""
            SELECT * FROM licenses WHERE license_key=?
        """, (key,)).fetchone()

        if not license_row:
            await update.message.reply_text("Invalid license key.")
            return

        if license_row["used_by"] is not None:
            await update.message.reply_text("This license key has already been used.")
            return

        user_row = con.execute("""
            SELECT vip_until FROM users WHERE telegram_id=?
        """, (user.id,)).fetchone()

        if not user_row:
            await update.message.reply_text("Send /start first.")
            return

        expiry = max(now, int(user_row["vip_until"] or 0))
        expiry += int(license_row["days"]) * 86400

        con.execute("""
            UPDATE users SET vip_until=? WHERE telegram_id=?
        """, (expiry, user.id))

        con.execute("""
            UPDATE licenses SET used_by=?,used_at=?
            WHERE license_key=? AND used_by IS NULL
        """, (user.id, now, key))

    audit(user.id, "license_redeemed", key)
    await update.message.reply_text(
        f"👑 License activated!\nVIP duration: {license_row['days']} days."
    )


# =========================================================
# HEALTH CHECK + STARTUP
# =========================================================

@app.route("/health")
def health():
    return {
        "status": "ok",
        "telegram_ready": telegram_ready,
        "telegram_error": telegram_start_error
    }, 200


def main():
    init_db()

    if not ADMIN_PASSWORD:
        log.warning("ADMIN_PANEL_PASSWORD is missing. Admin login will not work.")

    if not BREVO_API_KEY or not SENDER_EMAIL:
        log.warning("Email OTP is unavailable until Brevo is configured.")

    if not GEMINI_API_KEY:
        log.warning("Gemini AI is unavailable until GEMINI_API_KEY is configured.")

    if BOT_TOKEN:
        threading.Thread(
            target=start_telegram,
            daemon=True,
            name="telegram-bot"
        ).start()
    else:
        log.error("BOT_TOKEN missing. Web server will start without Telegram.")

    log.info("Starting SUPER AI BOT V3 web server on port %s", PORT)
    serve(app, host="0.0.0.0", port=PORT, threads=8)


if __name__ == "__main__":
    main()
