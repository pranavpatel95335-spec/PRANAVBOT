
import os
import re
import time
import hmac
import html
import json
import uuid
import base64
import hashlib
import secrets
import logging
import sqlite3
import asyncio
import threading
from datetime import datetime, timedelta
from functools import wraps

import requests
from flask import Flask, request, session, redirect, url_for, abort
from waitress import serve

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

# =========================================================
# CONFIG
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("SUPER_AI")

web = Flask(__name__)
web.secret_key = SECRET_KEY
web.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12)
)

bot_app = None
telegram_ready = False
telegram_error = None

# =========================================================
# DATABASE
# =========================================================

def connect():
    con = sqlite3.connect(DATABASE, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db():
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
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

        CREATE TABLE IF NOT EXISTS web_users(
            email TEXT PRIMARY KEY,
            verified INTEGER DEFAULT 0,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS otps(
            email TEXT NOT NULL,
            purpose TEXT NOT NULL,
            otp_hash TEXT NOT NULL,
            expires INTEGER NOT NULL,
            attempts INTEGER DEFAULT 0,
            last_sent INTEGER NOT NULL,
            PRIMARY KEY(email,purpose)
        );

        CREATE TABLE IF NOT EXISTS chats(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS licenses(
            license_key TEXT PRIMARY KEY,
            days INTEGER NOT NULL,
            used_by INTEGER,
            created_at INTEGER NOT NULL,
            used_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS audit(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT DEFAULT '',
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS feature_flags(
            feature TEXT PRIMARY KEY,
            enabled INTEGER DEFAULT 1
        );

        CREATE INDEX IF NOT EXISTS chats_user_idx
        ON chats(telegram_id,id);
        """)

        defaults = {
            "welcome": "Welcome to SUPER AI BOT V3 GOLDEN VIP EDITION!",
            "personality": "You are a helpful, friendly and accurate AI assistant.",
            "daily_limit": "30",
            "vip_daily_limit": "300",
            "ai_enabled": "1",
            "image_enabled": "0",
            "maintenance": "0",
            "menu_title": "🌟 SUPER AI BOT V3"
        }

        for k, v in defaults.items():
            con.execute(
                "INSERT OR IGNORE INTO settings VALUES(?,?)", (k, v)
            )

        for name in ("ai", "image", "profile", "vip", "history"):
            con.execute(
                "INSERT OR IGNORE INTO feature_flags VALUES(?,1)", (name,)
            )


def get_setting(key, default=""):
    with connect() as con:
        row = con.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with connect() as con:
        con.execute("""
        INSERT INTO settings(key,value) VALUES(?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, str(value)))


def audit(actor, action, detail=""):
    with connect() as con:
        con.execute("""
        INSERT INTO audit(actor,action,detail,created_at)
        VALUES(?,?,?,?)
        """, (str(actor), action, str(detail)[:1000], int(time.time())))


def feature_enabled(name):
    with connect() as con:
        row = con.execute(
            "SELECT enabled FROM feature_flags WHERE feature=?", (name,)
        ).fetchone()
    return bool(row["enabled"]) if row else True


def set_feature(name, enabled):
    with connect() as con:
        con.execute("""
        INSERT INTO feature_flags(feature,enabled) VALUES(?,?)
        ON CONFLICT(feature) DO UPDATE SET enabled=excluded.enabled
        """, (name, int(enabled)))


def save_user(user):
    with connect() as con:
        con.execute("""
        INSERT INTO users(telegram_id,username,first_name,joined_at)
        VALUES(?,?,?,?)
        ON CONFLICT(telegram_id) DO UPDATE SET
        username=excluded.username,
        first_name=excluded.first_name
        """, (
            user.id, user.username or "", user.first_name or "",
            int(time.time())
        ))


def get_user(tg_id):
    with connect() as con:
        return con.execute(
            "SELECT * FROM users WHERE telegram_id=?", (tg_id,)
        ).fetchone()


def is_vip(tg_id):
    user = get_user(tg_id)
    return bool(user and int(user["vip_until"] or 0) > int(time.time()))


def allowed_user(tg_id):
    user = get_user(tg_id)

    if not user:
        return False, "Please send /start first."

    if user["banned"]:
        return False, "Your account is restricted."

    if get_setting("maintenance", "0") == "1" and str(tg_id) != ADMIN_ID:
        return False, "Bot is under maintenance."

    return True, ""


def daily_count(tg_id, role):
    now = datetime.now()
    midnight = int(now.replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp())

    with connect() as con:
        row = con.execute("""
        SELECT COUNT(*) AS total FROM chats
        WHERE telegram_id=? AND role=? AND created_at>=?
        """, (tg_id, role, midnight)).fetchone()

    return row["total"]


def save_chat(tg_id, role, content):
    with connect() as con:
        con.execute("""
        INSERT INTO chats(telegram_id,role,content,created_at)
        VALUES(?,?,?,?)
        """, (tg_id, role, content[:12000], int(time.time())))


def history(tg_id, limit=8):
    with connect() as con:
        rows = con.execute("""
        SELECT role,content FROM chats
        WHERE telegram_id=?
        ORDER BY id DESC LIMIT ?
        """, (tg_id, limit)).fetchall()

    return list(reversed(rows))


# =========================================================
# EMAIL OTP
# =========================================================

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def send_email(email, subject, message):
    if not BREVO_API_KEY or not SENDER_EMAIL:
        return False, "Brevo is not configured."

    try:
        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "api-key": BREVO_API_KEY,
                "Content-Type": "application/json",
                "accept": "application/json"
            },
            json={
                "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
                "to": [{"email": email}],
                "subject": subject,
                "textContent": message
            },
            timeout=20
        )

        if response.status_code in (200, 201, 202):
            return True, "Sent"

        log.error("Brevo response: %s", response.text[:500])
        return False, "Brevo rejected the request."

    except requests.RequestException:
        log.exception("Email sending error")
        return False, "Email service unavailable."


def make_otp_hash(email, purpose, otp):
    value = f"{email}:{purpose}:{otp}:{SECRET_KEY}"
    return hashlib.sha256(value.encode()).hexdigest()


def send_otp(email, purpose):
    email = email.strip().lower()

    if not EMAIL_RE.match(email):
        return False, "Invalid email address."

    now = int(time.time())

    with connect() as con:
        old = con.execute("""
        SELECT last_sent FROM otps WHERE email=? AND purpose=?
        """, (email, purpose)).fetchone()

    if old and now - old["last_sent"] < 60:
        return False, "Wait 60 seconds before requesting another OTP."

    otp = f"{secrets.randbelow(1000000):06d}"

    ok, message = send_email(
        email,
        "SUPER AI BOT verification code",
        f"Your OTP is {otp}. It expires in 5 minutes. Do not share it."
    )

    if not ok:
        return False, message

    with connect() as con:
        con.execute("""
        INSERT INTO otps(email,purpose,otp_hash,expires,attempts,last_sent)
        VALUES(?,?,?,?,0,?)
        ON CONFLICT(email,purpose) DO UPDATE SET
        otp_hash=excluded.otp_hash,
        expires=excluded.expires,
        attempts=0,
        last_sent=excluded.last_sent
        """, (
            email, purpose, make_otp_hash(email, purpose, otp),
            now + 300, now
        ))

    return True, "OTP sent."


def verify_otp(email, purpose, otp):
    email = email.strip().lower()
    now = int(time.time())

    with connect() as con:
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

        expected = make_otp_hash(email, purpose, otp.strip())

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

def ask_gemini(tg_id, message):
    if not GEMINI_API_KEY:
        return "Gemini API key is not configured."

    user = get_user(tg_id)

    if not user or not user["email_verified"]:
        return "Verify your email first using /link."

    if get_setting("ai_enabled", "1") != "1" or not feature_enabled("ai"):
        return "AI Chat is disabled."

    limit = int(
        get_setting("vip_daily_limit", "300")
        if is_vip(tg_id)
        else get_setting("daily_limit", "30")
    )

    if daily_count(tg_id, "user") >= limit:
        return "Your daily AI limit has been reached."

    contents = []

    for item in history(tg_id):
        contents.append({
            "role": "model" if item["role"] == "assistant" else "user",
            "parts": [{"text": item["content"]}]
        })

    contents.append({
        "role": "user",
        "parts": [{"text": message[:8000]}]
    })

    payload = {
        "system_instruction": {
            "parts": [{"text": get_setting("personality")}]
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
            log.error("Gemini error: %s", response.text[:500])
            return "Gemini returned an error. Please try again later."

        data = response.json()
        parts = data.get("candidates", [{}])[0].get(
            "content", {}
        ).get("parts", [])

        answer = "\n".join(
            p.get("text", "") for p in parts if p.get("text")
        ).strip()

        if not answer:
            return "AI returned an empty response."

        save_chat(tg_id, "user", message)
        save_chat(tg_id, "assistant", answer)

        with connect() as con:
            con.execute(
                "UPDATE users SET ai_count=ai_count+1 WHERE telegram_id=?",
                (tg_id,)
            )

        return answer[:12000]

    except requests.RequestException:
        log.exception("Gemini request failed")
        return "AI service is temporarily unavailable."


# =========================================================
# IMAGE GENERATION
# =========================================================

def create_image(prompt):
    if not GEMINI_API_KEY or not GEMINI_IMAGE_MODEL:
        return None, "Image model is not configured."

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + GEMINI_IMAGE_MODEL + ":generateContent"
    )

    try:
        response = requests.post(
            url,
            headers={"x-goog-api-key": GEMINI_API_KEY},
            json={
                "contents": [{"parts": [{"text": prompt[:2000]}]}],
                "generationConfig": {
                    "responseModalities": ["TEXT", "IMAGE"]
                }
            },
            timeout=120
        )

        if response.status_code != 200:
            log.error("Image API error: %s", response.text[:500])
            return None, "Image model rejected the request."

        parts = response.json().get("candidates", [{}])[0].get(
            "content", {}
        ).get("parts", [])

        for part in parts:
            item = part.get("inlineData") or part.get("inline_data")
            if item and item.get("data"):
                return base64.b64decode(item["data"]), None

        return None, "No image was returned by the model."

    except Exception:
        log.exception("Image generation error")
        return None, "Image service unavailable."


# =========================================================
# TELEGRAM MENUS
# =========================================================

def keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🤖 AI CHAT", callback_data="ai"),
            InlineKeyboardButton("🎨 IMAGE STUDIO", callback_data="image")
        ],
        [
            InlineKeyboardButton("👤 PROFILE", callback_data="profile"),
            InlineKeyboardButton("👑 VIP", callback_data="vip")
        ],
        [
            InlineKeyboardButton("📜 HISTORY", callback_data="history"),
            InlineKeyboardButton("⚙️ SETTINGS", callback_data="settings")
        ],
        [InlineKeyboardButton("❓ HELP", callback_data="help")]
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user)

    await update.message.reply_text(
        f"{get_setting('menu_title')}\n\n"
        f"Welcome {user.first_name}!\n\n"
        f"{get_setting('welcome')}\n\n"
        "Verify your email with /link to unlock AI features.",
        reply_markup=keyboard()
    )


async def link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user)

    code = f"{secrets.randbelow(1000000):06d}"

    with connect() as con:
        con.execute("""
        UPDATE users SET link_code=?,link_expires=?
        WHERE telegram_id=?
        """, (code, int(time.time()) + 600, user.id))

    await update.message.reply_text(
        "🔐 TELEGRAM LINK CODE\n\n"
        f"Your code: {code}\n\n"
        "1. Open the website.\n"
        "2. Verify your email.\n"
        "3. Enter this code in Link Telegram Account.\n\n"
        "Expires in 10 minutes. Do not share it."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Operation cancelled.")


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_user(update.effective_user)
    await show_profile(update, update.effective_user.id)


async def show_profile(update, tg_id):
    user = get_user(tg_id)

    if not user:
        return

    text = (
        "👤 YOUR PROFILE\n\n"
        f"Telegram ID: {tg_id}\n"
        f"Username: @{user['username'] or 'Not set'}\n"
        f"Email: {user['email'] or 'Not linked'}\n"
        f"Email verified: {'Yes' if user['email_verified'] else 'No'}\n"
        f"Membership: {'VIP' if is_vip(tg_id) else 'Standard'}\n"
        f"AI requests: {user['ai_count']}\n"
        f"Image requests: {user['image_count']}"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=keyboard()
        )
    else:
        await update.message.reply_text(text, reply_markup=keyboard())


async def vip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👑 VIP MEMBERSHIP\n\n"
        "VIP members receive a higher AI usage limit.\n"
        "Contact the administrator for membership."
    )


async def image_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user)

    ok, reason = allowed_user(user.id)
    if not ok:
        await update.message.reply_text(reason)
        return

    record = get_user(user.id)

    if not record["email_verified"]:
        await update.message.reply_text("Verify your email using /link first.")
        return

    if get_setting("image_enabled", "0") != "1" or not feature_enabled("image"):
        await update.message.reply_text("Image Studio is disabled.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: /image describe your image"
        )
        return

    limit = 20 if is_vip(user.id) else 3

    if daily_count(user.id, "image") >= limit:
        await update.message.reply_text("Daily image limit reached.")
        return

    await update.message.reply_text("🎨 Creating image...")

    data, error = create_image(" ".join(context.args))

    if error:
        await update.message.reply_text(error)
        return

    import io
    await update.message.reply_photo(
        photo=io.BytesIO(data),
        caption="SUPER AI BOT V3"
    )

    save_chat(user.id, "image", " ".join(context.args))

    with connect() as con:
        con.execute(
            "UPDATE users SET image_count=image_count+1 WHERE telegram_id=?",
            (user.id,)
        )


async def redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user)

    if not context.args:
        await update.message.reply_text(
            "Usage: /redeem YOUR_LICENSE_KEY"
        )
        return

    key = context.args[0].strip().upper()
    now = int(time.time())

    with connect() as con:
        item = con.execute(
            "SELECT * FROM licenses WHERE license_key=?", (key,)
        ).fetchone()

        if not item:
            await update.message.reply_text("Invalid license key.")
            return

        if item["used_by"] is not None:
            await update.message.reply_text("This license is already used.")
            return

        row = con.execute(
            "SELECT vip_until FROM users WHERE telegram_id=?", (user.id,)
        ).fetchone()

        expiry = max(now, int(row["vip_until"] or 0))
        expiry += int(item["days"]) * 86400

        changed = con.execute("""
        UPDATE licenses SET used_by=?,used_at=?
        WHERE license_key=? AND used_by IS NULL
        """, (user.id, now, key)).rowcount

        if changed != 1:
            await update.message.reply_text("License redemption failed.")
            return

        con.execute(
            "UPDATE users SET vip_until=? WHERE telegram_id=?",
            (expiry, user.id)
        )

    audit(user.id, "license_redeemed", key)
    await update.message.reply_text(
        f"👑 VIP activated for {item['days']} days."
    )


# =========================================================
# TELEGRAM ADMIN COMMANDS
# =========================================================

def is_admin(tg_id):
    return bool(ADMIN_ID and str(tg_id) == ADMIN_ID)


async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Access denied.")
        return

    await update.message.reply_text(
        "ADMIN COMMANDS\n\n"
        "/stats\n"
        "/ban USER_ID\n"
        "/unban USER_ID\n"
        "/vipadd USER_ID DAYS\n"
        "/broadcast MESSAGE"
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    with connect() as con:
        total = con.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        verified = con.execute(
            "SELECT COUNT(*) n FROM users WHERE email_verified=1"
        ).fetchone()["n"]
        banned = con.execute(
            "SELECT COUNT(*) n FROM users WHERE banned=1"
        ).fetchone()["n"]

    await update.message.reply_text(
        f"Users: {total}\nVerified: {verified}\nBanned: {banned}"
    )


async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /ban USER_ID")
        return

    tg_id = int(context.args[0])

    with connect() as con:
        con.execute(
            "UPDATE users SET banned=1 WHERE telegram_id=?", (tg_id,)
        )

    audit(ADMIN_ID, "ban", tg_id)
    await update.message.reply_text("User banned.")


async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /unban USER_ID")
        return

    tg_id = int(context.args[0])

    with connect() as con:
        con.execute(
            "UPDATE users SET banned=0 WHERE telegram_id=?", (tg_id,)
        )

    audit(ADMIN_ID, "unban", tg_id)
    await update.message.reply_text("User unbanned.")


async def vip_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if len(context.args) != 2:
        await update.message.reply_text("Usage: /vipadd USER_ID DAYS")
        return

    try:
        tg_id, days = int(context.args[0]), int(context.args[1])
        if days < 1 or days > 36500:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Invalid ID or duration.")
        return

    with connect() as con:
        row = con.execute(
            "SELECT vip_until FROM users WHERE telegram_id=?", (tg_id,)
        ).fetchone()

        if not row:
            await update.message.reply_text("User not found.")
            return

        expiry = max(int(time.time()), int(row["vip_until"] or 0))
        expiry += days * 86400

        con.execute(
            "UPDATE users SET vip_until=? WHERE telegram_id=?",
            (expiry, tg_id)
        )

    audit(ADMIN_ID, "vip_add", f"{tg_id}:{days}")
    await update.message.reply_text("VIP updated.")


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    message = " ".join(context.args).strip()

    if not message:
        await update.message.reply_text("Usage: /broadcast MESSAGE")
        return

    with connect() as con:
        users = con.execute(
            "SELECT telegram_id FROM users WHERE banned=0"
        ).fetchall()

    sent = 0
    failed = 0

    for row in users:
        try:
            await context.bot.send_message(
                row["telegram_id"], message[:4000]
            )
            sent += 1
        except Exception:
            failed += 1

        await asyncio.sleep(0.05)

    audit(ADMIN_ID, "broadcast", f"{sent} sent, {failed} failed")
    await update.message.reply_text(
        f"Broadcast complete.\nSent: {sent}\nFailed: {failed}"
    )


# =========================================================
# TELEGRAM CALLBACKS AND TEXT
# =========================================================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    tg_id = query.from_user.id
    save_user(query.from_user)

    if query.data == "profile":
        await show_profile(update, tg_id)

    elif query.data == "ai":
        context.user_data["mode"] = "ai"
        await query.edit_message_text(
            "🤖 AI CHAT\n\nSend your message.\nUse /cancel to exit."
        )

    elif query.data == "image":
        context.user_data["mode"] = "image"
        await query.edit_message_text(
            "🎨 IMAGE STUDIO\n\nDescribe your image.\nUse /cancel to exit."
        )

    elif query.data == "vip":
        await query.edit_message_text(
            "👑 VIP MEMBERSHIP\n\n"
            "Use /redeem KEY to activate a license.",
            reply_markup=keyboard()
        )

    elif query.data == "history":
        rows = history(tg_id, 10)
        text = "📜 RECENT HISTORY\n\n"

        for row in rows:
            text += f"{row['role']}: {row['content'][:250]}\n\n"

        await query.edit_message_text(
            text[:4000] if rows else "No history found.",
            reply_markup=keyboard()
        )

    elif query.data == "settings":
        await query.edit_message_text(
            "⚙️ SETTINGS\n\nUse /link to verify your email.",
            reply_markup=keyboard()
        )

    elif query.data == "help":
        await query.edit_message_text(
            "/start - Main menu\n"
            "/link - Email linking\n"
            "/profile - Profile\n"
            "/vip - VIP information\n"
            "/image - Image command\n"
            "/redeem KEY - Redeem license\n"
            "/cancel - Cancel mode",
            reply_markup=keyboard()
        )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user)

    ok, reason = allowed_user(user.id)
    if not ok:
        await update.message.reply_text(reason)
        return

    record = get_user(user.id)

    if not record["email_verified"]:
        await update.message.reply_text(
            "Verify your email first using /link."
        )
        return

    mode = context.user_data.get("mode", "ai")

    if mode == "image":
        if get_setting("image_enabled", "0") != "1":
            await update.message.reply_text("Image Studio is disabled.")
            return

        limit = 20 if is_vip(user.id) else 3

        if daily_count(user.id, "image") >= limit:
            await update.message.reply_text("Daily image limit reached.")
            return

        await update.message.chat.send_action(ChatAction.UPLOAD_PHOTO)

        data, error = create_image(update.message.text)

        if error:
            await update.message.reply_text(error)
            return

        import io
        await update.message.reply_photo(
            photo=io.BytesIO(data), caption="SUPER AI BOT V3"
        )

        save_chat(user.id, "image", update.message.text)
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    answer = ask_gemini(user.id, update.message.text)
    await update.message.reply_text(answer)


# =========================================================
# TELEGRAM STARTUP - PYTHON 3.14 EVENT LOOP FIX
# =========================================================

async def post_init(application):
    global telegram_ready
    telegram_ready = True
    log.info("Telegram bot initialized.")


async def post_shutdown(application):
    global telegram_ready
    telegram_ready = False


def start_telegram():
    global bot_app, telegram_error

    if not BOT_TOKEN:
        telegram_error = "BOT_TOKEN is missing."
        log.error(telegram_error)
        return

    # Critical: create and install an event loop in this thread.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        bot_app = (
            Application.builder()
            .token(BOT_TOKEN)
            .post_init(post_init)
            .post_shutdown(post_shutdown)
            .build()
        )

        bot_app.add_handler(CommandHandler("start", start))
        bot_app.add_handler(CommandHandler("link", link))
        bot_app.add_handler(CommandHandler("cancel", cancel))
        bot_app.add_handler(CommandHandler("profile", profile))
        bot_app.add_handler(CommandHandler("vip", vip))
        bot_app.add_handler(CommandHandler("image", image_command))
        bot_app.add_handler(CommandHandler("redeem", redeem))

        bot_app.add_handler(CommandHandler("admin", admin_help))
        bot_app.add_handler(CommandHandler("stats", stats))
        bot_app.add_handler(CommandHandler("ban", ban_user))
        bot_app.add_handler(CommandHandler("unban", unban_user))
        bot_app.add_handler(CommandHandler("vipadd", vip_add))
        bot_app.add_handler(CommandHandler("broadcast", broadcast))

        bot_app.add_handler(CallbackQueryHandler(callback_handler))
        bot_app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
        )

        bot_app.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
            close_loop=False
        )

    except Exception as exc:
        telegram_error = str(exc)
        log.exception("Telegram startup failed")

    finally:
        global telegram_ready
        telegram_ready = False

        try:
            if bot_app and bot_app.running:
                loop.run_until_complete(bot_app.stop())
                loop.run_until_complete(bot_app.shutdown())
        except Exception:
            log.exception("Telegram shutdown error")

        asyncio.set_event_loop(None)

        if not loop.is_closed():
            loop.close()


# =========================================================
# WEB APP
# =========================================================

STYLE = """
<style>
*{box-sizing:border-box}
body{margin:0;padding:20px;background:#080d1c;color:#eef2ff;
font-family:Arial,sans-serif}
.wrap{max-width:1050px;margin:auto}
.card{background:#111b32;border:1px solid #28395e;
border-radius:16px;padding:20px;margin:14px 0;overflow-wrap:anywhere}
h1,h2{color:#f5ca70}
input,textarea,select{width:100%;padding:12px;margin:7px 0;
border-radius:9px;border:1px solid #435477;background:#091225;color:white}
button,.btn{display:inline-block;padding:11px 16px;background:#bd913d;
color:#111;border:0;border-radius:9px;font-weight:bold;text-decoration:none;
cursor:pointer;margin:4px 0}
a{color:#f5cf78}
table{width:100%;border-collapse:collapse;display:block;overflow-x:auto}
td,th{padding:9px;border-bottom:1px solid #34415b;text-align:left}
</style>
"""


def csrf():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(24)
    return session["_csrf"]


def check_csrf():
    supplied = request.form.get("_csrf", "")
    expected = session.get("_csrf", "")

    if not expected or not hmac.compare_digest(supplied, expected):
        abort(403)


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect("/admin")
        return fn(*args, **kwargs)
    return wrapper


def web_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("email"):
            return redirect("/login")
        return fn(*args, **kwargs)
    return wrapper


def page(title, body):
    from flask import render_template_string

    return render_template_string(
        "<!doctype html><html><head>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>{{title}}</title>" + STYLE +
        "</head><body><div class='wrap'>"
        "<h1>🌟 SUPER AI BOT V3 GOLDEN VIP</h1>"
        "{{body|safe}}</div></body></html>",
        title=title, body=body
    )


@web.route("/")
def home():
    status = "Online" if telegram_ready else "Telegram not connected"

    return page("Home", f"""
    <div class="card">
    <h2>GOLDEN VIP EDITION</h2>
    <p>AI Assistant • Image Studio • VIP Membership</p>
    <p>Telegram: {html.escape(status)}</p>
    <a class="btn" href="/login">User Login</a>
    <a class="btn" href="/admin">Admin Panel</a>
    </div>
    """)


@web.route("/login", methods=["GET", "POST"])
def login():
    message = ""

    if request.method == "POST":
        check_csrf()

        email = request.form.get("email", "").strip().lower()
        action = request.form.get("action", "")

        if not EMAIL_RE.match(email):
            message = "Invalid email."

        elif action == "send":
            ok, result = send_otp(email, "website")
            message = result

        elif action == "verify":
            if verify_otp(email, "website", request.form.get("otp", "")):
                with connect() as con:
                    con.execute("""
                    INSERT INTO web_users(email,verified,created_at)
                    VALUES(?,1,?)
                    ON CONFLICT(email) DO UPDATE SET verified=1
                    """, (email, int(time.time())))

                session.clear()
                session.permanent = True
                session["email"] = email
                return redirect("/profile")

            message = "Incorrect or expired OTP."

        elif action == "link":
            code = request.form.get("link_code", "").strip()

            if not re.fullmatch(r"\d{6}", code):
                message = "Enter the six-digit Telegram code."
            else:
                with connect() as con:
                    target = con.execute("""
                    SELECT telegram_id FROM users
                    WHERE link_code=? AND link_expires>=?
                    """, (code, int(time.time()))).fetchone()

                    verified = con.execute(
                        "SELECT verified FROM web_users WHERE email=?",
                        (email,)
                    ).fetchone()

                    if not verified or not verified["verified"]:
                        message = "Verify this email first."
                    elif not target:
                        message = "Invalid or expired Telegram code."
                    else:
                        con.execute("""
                        UPDATE users SET email=?,email_verified=1,
                        link_code='',link_expires=0
                        WHERE telegram_id=?
                        """, (email, target["telegram_id"]))

                        audit(email, "telegram_link", target["telegram_id"])
                        message = "Telegram account linked successfully."

    token = csrf()

    return page("Email Login", f"""
    <div class="card">
    <h2>📧 EMAIL VERIFICATION</h2>
    <p>{html.escape(message)}</p>
    <form method="post">
      <input type="hidden" name="_csrf" value="{token}">
      <input name="email" type="email" placeholder="Email address" required>
      <button name="action" value="send">Send OTP</button>
      <input name="otp" maxlength="6" placeholder="6 digit OTP">
      <button name="action" value="verify">Verify Email</button>
    </form>
    </div>
    <div class="card">
    <h2>🔗 LINK TELEGRAM</h2>
    <p>Verify your email first. Then send /link to the Telegram bot.</p>
    <form method="post">
      <input type="hidden" name="_csrf" value="{token}">
      <input name="email" type="email" placeholder="Verified email" required>
      <input name="link_code" maxlength="6" placeholder="Telegram link code" required>
      <button name="action" value="link">Link Account</button>
    </form>
    </div>
    """)


@web.route("/profile")
@web_required
def user_profile():
    email = session["email"]

    with connect() as con:
        user = con.execute(
            "SELECT * FROM users WHERE email=?", (email,)
        ).fetchone()

    if user:
        info = (
            f"Telegram ID: {user['telegram_id']}<br>"
            f"Username: @{html.escape(user['username'] or '-')}"
            f"<br>AI requests: {user['ai_count']}"
            f"<br>Image requests: {user['image_count']}"
            f"<br>VIP: {'Active' if is_vip(user['telegram_id']) else 'Standard'}"
        )
    else:
        info = "No Telegram account linked. Send /link to the bot."

    return page("Profile", f"""
    <div class="card">
    <h2>YOUR PROFILE</h2>
    <p>{html.escape(email)}</p>
    <p>{info}</p>
    <a class="btn" href="/logout">Logout</a>
    </div>
    """)


@web.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@web.route("/admin", methods=["GET", "POST"])
def admin_login():
    message = ""

    if request.method == "POST":
        check_csrf()
        password = request.form.get("password", "")

        if ADMIN_PASSWORD and hmac.compare_digest(
            password, ADMIN_PASSWORD
        ):
            session.clear()
            session.permanent = True
            session["admin"] = True
            audit("web_admin", "login")
            return redirect("/admin/dashboard")

        message = "Invalid admin password."

    token = csrf()

    return page("Admin Login", f"""
    <div class="card">
    <h2>🔐 ADMIN LOGIN</h2>
    <p>{html.escape(message)}</p>
    <form method="post">
    <input type="hidden" name="_csrf" value="{token}">
    <input type="password" name="password" placeholder="Admin password" required>
    <button>Login</button>
    </form>
    </div>
    """)


@web.route("/admin/logout")
@admin_required
def admin_logout():
    session.clear()
    return redirect("/admin")


@web.route("/admin/dashboard", methods=["GET", "POST"])
@admin_required
def dashboard():
    message = ""

    if request.method == "POST":
        check_csrf()
        action = request.form.get("action", "")

        try:
            if action == "settings":
                keys = (
                    "welcome", "personality", "menu_title",
                    "daily_limit", "vip_daily_limit",
                    "ai_enabled", "image_enabled", "maintenance"
                )

                for key in keys:
                    if key not in request.form:
                        continue

                    value = request.form[key].strip()

                    if key in ("daily_limit", "vip_daily_limit"):
                        value = str(max(1, min(10000, int(value))))

                    if key in ("ai_enabled", "image_enabled", "maintenance"):
                        value = "1" if value == "1" else "0"

                    set_setting(key, value)

                audit("web_admin", "settings_updated")
                message = "Settings saved."

            elif action == "user":
                tg_id = int(request.form["telegram_id"])
                operation = request.form["operation"]

                if operation not in ("ban", "unban", "remove_vip"):
                    raise ValueError("Invalid operation.")

                column, value = {
                    "ban": ("banned", 1),
                    "unban": ("banned", 0),
                    "remove_vip": ("vip_until", 0)
                }[operation]

                with connect() as con:
                    con.execute(
                        f"UPDATE users SET {column}=? WHERE telegram_id=?",
                        (value, tg_id)
                    )

                audit("web_admin", operation, tg_id)
                message = "User updated."

            elif action == "vip":
                tg_id = int(request.form["telegram_id"])
                days = int(request.form["days"])

                if not 1 <= days <= 36500:
                    raise ValueError("Invalid VIP duration.")

                with connect() as con:
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
                message = "VIP updated."

            elif action == "license":
                days = int(request.form["days"])
                quantity = int(request.form.get("quantity", "1"))

                if not 1 <= days <= 36500 or not 1 <= quantity <= 50:
                    raise ValueError("Invalid days or quantity.")

                keys = []

                with connect() as con:
                    for _ in range(quantity):
                        key = "GOLD-" + secrets.token_hex(8).upper()
                        con.execute("""
                        INSERT INTO licenses(license_key,days,created_at)
                        VALUES(?,?,?)
                        """, (key, days, int(time.time())))
                        keys.append(key)

                audit("web_admin", "licenses_created", quantity)
                message = "Generated keys: " + ", ".join(keys)

            elif action == "feature":
                name = request.form["feature"]
                if name not in ("ai", "image", "profile", "vip", "history"):
                    raise ValueError("Invalid feature.")

                set_feature(name, request.form.get("enabled") == "1")
                audit("web_admin", "feature_changed", name)
                message = "Feature updated."

            elif action == "message":
                tg_id = int(request.form["telegram_id"])
                text = request.form["message"].strip()

                if not text or not bot_app or not telegram_ready:
                    raise ValueError("Bot unavailable or message empty.")

                bot_app.create_task(
                    bot_app.bot.send_message(tg_id, text[:4000])
                )
                audit("web_admin", "individual_message", tg_id)
                message = "Message queued."

            elif action == "broadcast":
                text = request.form["message"].strip()

                if not text or not bot_app or not telegram_ready:
                    raise ValueError("Bot unavailable or message empty.")

                bot_app.create_task(web_broadcast(text))
                audit("web_admin", "broadcast_queued")
                message = "Broadcast queued."

        except (ValueError, KeyError) as exc:
            message = str(exc)
        except Exception:
            log.exception("Admin action failed")
            message = "Action failed. Check Render logs."

    with connect() as con:
        total = con.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        verified = con.execute(
            "SELECT COUNT(*) n FROM users WHERE email_verified=1"
        ).fetchone()["n"]
        banned = con.execute(
            "SELECT COUNT(*) n FROM users WHERE banned=1"
        ).fetchone()["n"]

        users = con.execute("""
        SELECT telegram_id,username,email,email_verified,banned
        FROM users ORDER BY joined_at DESC LIMIT 100
        """).fetchall()

        logs = con.execute("""
        SELECT actor,action,detail FROM audit
        ORDER BY id DESC LIMIT 20
        """).fetchall()

        licenses = con.execute("""
        SELECT license_key,days,used_by FROM licenses
        ORDER BY created_at DESC LIMIT 20
        """).fetchall()

    token = csrf()

    user_rows = "".join(
        "<tr><td>" + str(u["telegram_id"]) +
        "</td><td>" + html.escape(u["username"] or "-") +
        "</td><td>" + html.escape(u["email"] or "-") +
        "</td><td>" + ("Yes" if u["email_verified"] else "No") +
        "</td><td>" + ("Banned" if u["banned"] else "Active") +
        "</td></tr>"
        for u in users
    )

    log_rows = "".join(
        "<tr><td>" + html.escape(x["actor"]) +
        "</td><td>" + html.escape(x["action"]) +
        "</td><td>" + html.escape(x["detail"]) + "</td></tr>"
        for x in logs
    )

    license_rows = "".join(
        "<tr><td>" + html.escape(x["license_key"]) +
        "</td><td>" + str(x["days"]) +
        "</td><td>" + str(x["used_by"] or "Unused") + "</td></tr>"
        for x in licenses
    )

    body = f"""
    <div class="card">
    <h2>👑 ADMIN DASHBOARD</h2>
    <p>{html.escape(message)}</p>
    <p>Total Users: {total} | Verified: {verified} | Banned: {banned}</p>
    <a class="btn" href="/admin/logout">Logout</a>
    </div>

    <div class="card"><h2>⚙️ BOT SETTINGS</h2>
    <form method="post">
    <input type="hidden" name="_csrf" value="{token}">
    <input type="hidden" name="action" value="settings">
    <label>Menu title</label>
    <input name="menu_title" value="{html.escape(get_setting('menu_title'))}">
    <label>Welcome</label>
    <textarea name="welcome">{html.escape(get_setting('welcome'))}</textarea>
    <label>AI personality</label>
    <textarea name="personality">{html.escape(get_setting('personality'))}</textarea>
    <label>Daily limit</label>
    <input name="daily_limit" value="{html.escape(get_setting('daily_limit'))}">
    <label>VIP daily limit</label>
    <input name="vip_daily_limit" value="{html.escape(get_setting('vip_daily_limit'))}">
    <label>AI enabled (1/0)</label>
    <input name="ai_enabled" value="{html.escape(get_setting('ai_enabled'))}">
    <label>Image enabled (1/0)</label>
    <input name="image_enabled" value="{html.escape(get_setting('image_enabled'))}">
    <label>Maintenance (1/0)</label>
    <input name="maintenance" value="{html.escape(get_setting('maintenance'))}">
    <button>Save Settings</button>
    </form></div>

    <div class="card"><h2>🧩 FEATURE CONTROL</h2>
    <form method="post">
    <input type="hidden" name="_csrf" value="{token}">
    <input type="hidden" name="action" value="feature">
    <select name="feature">
    <option value="ai">AI</option><option value="image">Image</option>
    <option value="profile">Profile</option><option value="vip">VIP</option>
    <option value="history">History</option></select>
    <select name="enabled"><option value="1">Enable</option>
    <option value="0">Disable</option></select>
    <button>Update Feature</button></form></div>

    <div class="card"><h2>👤 USER CONTROL</h2>
    <form method="post">
    <input type="hidden" name="_csrf" value="{token}">
    <input type="hidden" name="action" value="user">
    <input name="telegram_id" placeholder="Telegram ID" required>
    <select name="operation"><option value="ban">Ban</option>
    <option value="unban">Unban</option>
    <option value="remove_vip">Remove VIP</option></select>
    <button>Apply</button></form></div>

    <div class="card"><h2>👑 VIP MEMBERSHIP</h2>
    <form method="post">
    <input type="hidden" name="_csrf" value="{token}">
    <input type="hidden" name="action" value="vip">
    <input name="telegram_id" placeholder="Telegram ID" required>
    <input name="days" type="number" placeholder="Days" required>
    <button>Activate VIP</button></form></div>

    <div class="card"><h2>🔑 LICENSE GENERATOR</h2>
    <form method="post">
    <input type="hidden" name="_csrf" value="{token}">
    <input type="hidden" name="action" value="license">
    <input name="days" type="number" placeholder="Validity days" required>
    <input name="quantity" type="number" value="1" min="1" max="50">
    <button>Generate Keys</button></form>
    <table><tr><th>Key</th><th>Days</th><th>Used by</th></tr>
    {license_rows}</table></div>

    <div class="card"><h2>📨 MESSAGE USER</h2>
    <form method="post">
    <input type="hidden" name="_csrf" value="{token}">
    <input type="hidden" name="action" value="message">
    <input name="telegram_id" placeholder="Telegram ID" required>
    <textarea name="message" placeholder="Message" required></textarea>
    <button>Send</button></form></div>

    <div class="card"><h2>📢 BROADCAST</h2>
    <form method="post">
    <input type="hidden" name="_csrf" value="{token}">
    <input type="hidden" name="action" value="broadcast">
    <textarea name="message" placeholder="Message" required></textarea>
    <button>Broadcast</button></form></div>

    <div class="card"><h2>👥 USERS</h2>
    <table><tr><th>ID</th><th>Username</th><th>Email</th>
    <th>Verified</th><th>Status</th></tr>{user_rows}</table></div>

    <div class="card"><h2>🛡️ AUDIT LOG</h2>
    <table><tr><th>Actor</th><th>Action</th><th>Details</th></tr>
    {log_rows}</table></div>
    """

    return page("Admin Dashboard", body)


async def web_broadcast(message):
    if not bot_app:
        return

    with connect() as con:
        users = con.execute(
            "SELECT telegram_id FROM users WHERE banned=0"
        ).fetchall()

    for user in users:
        try:
            await bot_app.bot.send_message(
                user["telegram_id"], message[:4000]
            )
        except Exception:
            log.warning("Could not broadcast to %s", user["telegram_id"])

        await asyncio.sleep(0.05)


# =========================================================
# HEALTH + MAIN
# =========================================================

@web.route("/health")
def health():
    return {
        "status": "ok",
        "telegram_ready": telegram_ready,
        "telegram_error": telegram_error
    }, 200


def main():
    init_db()

    if not ADMIN_PASSWORD:
        log.warning("ADMIN_PANEL_PASSWORD is missing.")

    if not BREVO_API_KEY or not SENDER_EMAIL:
        log.warning("Email OTP is unavailable until Brevo is configured.")

    if not GEMINI_API_KEY:
        log.warning("Gemini API key is missing.")

    if BOT_TOKEN:
        threading.Thread(
            target=start_telegram,
            daemon=True,
            name="telegram-bot"
        ).start()
    else:
        log.error("BOT_TOKEN missing. Telegram will not start.")

    log.info("Starting Flask server on port %s", PORT)
    serve(web, host="0.0.0.0", port=PORT, threads=8)


if __name__ == "__main__":
    main()
