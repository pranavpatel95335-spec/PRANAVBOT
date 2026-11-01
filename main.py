
import os
import re
import hmac
import time
import html
import json
import base64
import hashlib
import secrets
import sqlite3
import asyncio
import threading
from datetime import datetime, timezone, timedelta
from io import BytesIO
from functools import wraps

import requests
from flask import Flask, request, session, redirect, url_for, render_template_string, jsonify, abort
from waitress import serve
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ============================================================
# SUPER AI BOT V3 - GOLDEN VIP EDITION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
ADMIN_PASSWORD = os.getenv("ADMIN_PANEL_PASSWORD", "")
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))

BREVO_API_KEY = os.getenv("BREVO_API_KEY", "").strip()
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "").strip()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "").strip()

DATABASE = os.getenv("DATABASE_PATH", "super_ai_bot.db")
PORT = int(os.getenv("PORT", "10000"))

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    MAX_CONTENT_LENGTH=2 * 1024 * 1024
)

telegram_app = None
telegram_loop = None
telegram_ready = False
startup_error = ""


# ---------------- DATABASE ----------------

def connect_db():
    con = sqlite3.connect(DATABASE, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with connect_db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            email TEXT UNIQUE,
            first_name TEXT DEFAULT '',
            username TEXT DEFAULT '',
            photo_url TEXT DEFAULT '',
            verified INTEGER DEFAULT 0,
            banned INTEGER DEFAULT 0,
            opted_in INTEGER DEFAULT 1,
            joined_at TEXT,
            last_seen TEXT,
            vip_until INTEGER DEFAULT 0,
            vip_plan TEXT DEFAULT 'FREE',
            language TEXT DEFAULT 'en',
            ai_count INTEGER DEFAULT 0,
            image_count INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS web_users (
            email TEXT PRIMARY KEY,
            verified INTEGER DEFAULT 0,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS otps (
            purpose TEXT,
            subject TEXT,
            digest TEXT,
            expires INTEGER,
            sent INTEGER,
            attempts INTEGER DEFAULT 0,
            PRIMARY KEY(purpose, subject)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            role TEXT,
            message TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT,
            action TEXT,
            details TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS licenses (
            code TEXT PRIMARY KEY,
            plan TEXT,
            days INTEGER,
            used_by INTEGER DEFAULT 0,
            expires INTEGER DEFAULT 0,
            created_at TEXT
        );
        """)

        defaults = {
            "ai_enabled": "1",
            "image_enabled": "0",
            "maintenance": "0",
            "welcome": "Welcome to SUPER AI BOT V3 — your premium AI workspace.",
            "ai_personality": (
                "You are SUPER AI, a polished, intelligent, professional assistant. "
                "Give accurate, helpful, clear and well-structured answers. "
                "Understand the user's context, avoid generic replies, and use headings "
                "or steps when useful. Be friendly, respectful and concise when appropriate. "
                "Never claim a task was completed unless it really was."
            ),
            "menu_title": "✦ GOLDEN AI STUDIO",
            "free_daily_limit": "30",
            "vip_daily_limit": "300"
        }

        for k, v in defaults.items():
            con.execute(
                "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                (k, v)
            )


def get_setting(key, default=""):
    with connect_db() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with connect_db() as con:
        con.execute("""
            INSERT INTO settings(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, str(value)))


def audit(action, details="", actor="ADMIN"):
    with connect_db() as con:
        con.execute(
            "INSERT INTO audit(actor,action,details,created_at) VALUES(?,?,?,?)",
            (actor, action, str(details)[:1500], datetime.now(timezone.utc).isoformat())
        )


def get_user(tg_id):
    with connect_db() as con:
        return con.execute("SELECT * FROM users WHERE telegram_id=?", (tg_id,)).fetchone()


def is_vip(user):
    return bool(user and user["vip_until"] and user["vip_until"] > int(time.time()))


def is_verified(tg_id):
    u = get_user(tg_id)
    return bool(u and u["verified"] and not u["banned"])


def upsert_telegram_user(tg_user):
    now = datetime.now(timezone.utc).isoformat()
    with connect_db() as con:
        con.execute("""
            INSERT INTO users(telegram_id,first_name,username,joined_at,last_seen)
            VALUES(?,?,?,?,?)
            ON CONFLICT(telegram_id) DO UPDATE SET
              first_name=excluded.first_name,
              username=excluded.username,
              last_seen=excluded.last_seen
        """, (
            tg_user.id,
            tg_user.first_name or "",
            tg_user.username or "",
            now, now
        ))


# ---------------- EMAIL OTP / BREVO ----------------

def email_valid(email):
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email or ""))


def otp_hash(purpose, subject, otp):
    raw = f"{purpose}:{subject}:{otp}".encode()
    return hmac.new(SECRET_KEY.encode(), raw, hashlib.sha256).hexdigest()


def brevo_send(to_email, subject, content):
    if not BREVO_API_KEY or not SENDER_EMAIL:
        raise RuntimeError("Brevo is not configured.")

    r = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={
            "api-key": BREVO_API_KEY,
            "accept": "application/json",
            "content-type": "application/json"
        },
        json={
            "sender": {"name": "SUPER AI BOT V3", "email": SENDER_EMAIL},
            "to": [{"email": to_email}],
            "subject": subject,
            "htmlContent": content
        },
        timeout=20
    )
    if r.status_code not in (200, 201, 202):
        raise RuntimeError(f"Brevo delivery failed: HTTP {r.status_code}")


def send_otp(purpose, email):
    now = int(time.time())

    with connect_db() as con:
        old = con.execute(
            "SELECT sent FROM otps WHERE purpose=? AND subject=?",
            (purpose, email)
        ).fetchone()

        if old and now - old["sent"] < 60:
            raise ValueError("Please wait 60 seconds before requesting another code.")

        code = f"{secrets.randbelow(1000000):06d}"
        digest = otp_hash(purpose, email, code)

        con.execute("""
            INSERT INTO otps(purpose,subject,digest,expires,sent,attempts)
            VALUES(?,?,?,?,?,0)
            ON CONFLICT(purpose,subject) DO UPDATE SET
              digest=excluded.digest,
              expires=excluded.expires,
              sent=excluded.sent,
              attempts=0
        """, (purpose, email, digest, now + 300, now))

    safe_code = html.escape(code)
    content = f"""
    <div style="font-family:Arial;background:#10131e;color:#fff;padding:28px;border-radius:16px">
      <h2 style="color:#e8c56f">SUPER AI BOT V3</h2>
      <p>Your email verification code:</p>
      <h1 style="letter-spacing:10px;color:#f5d98e">{safe_code}</h1>
      <p>This code expires in 5 minutes.</p>
      <small>If you did not request this code, ignore this email.</small>
    </div>
    """

    try:
        brevo_send(email, "SUPER AI BOT - Verification Code", content)
    except Exception:
        with connect_db() as con:
            con.execute("DELETE FROM otps WHERE purpose=? AND subject=?", (purpose, email))
        raise


def check_otp(purpose, email, code):
    if not re.fullmatch(r"\d{6}", code or ""):
        return False

    now = int(time.time())
    with connect_db() as con:
        row = con.execute(
            "SELECT * FROM otps WHERE purpose=? AND subject=?",
            (purpose, email)
        ).fetchone()

        if not row:
            return False

        if now > row["expires"] or row["attempts"] >= 5:
            con.execute("DELETE FROM otps WHERE purpose=? AND subject=?", (purpose, email))
            return False

        expected = otp_hash(purpose, email, code)
        if not hmac.compare_digest(expected, row["digest"]):
            con.execute("""
                UPDATE otps SET attempts=attempts+1
                WHERE purpose=? AND subject=?
            """, (purpose, email))
            return False

        con.execute("DELETE FROM otps WHERE purpose=? AND subject=?", (purpose, email))
        return True


# ---------------- GEMINI ----------------

def gemini_generate(prompt, system_instruction=None):
    if not GEMINI_API_KEY:
        raise RuntimeError("Gemini API key is missing.")

    system_instruction = system_instruction or get_setting("ai_personality")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )

    payload = {
        "systemInstruction": {
            "parts": [{"text": system_instruction[:8000]}]
        },
        "contents": [{
            "role": "user",
            "parts": [{"text": prompt[:12000]}]
        }],
        "generationConfig": {
            "temperature": 0.75,
            "maxOutputTokens": 1600
        }
    }

    r = requests.post(
        url,
        headers={"x-goog-api-key": GEMINI_API_KEY},
        json=payload,
        timeout=60
    )
    if r.status_code != 200:
        raise RuntimeError(f"Gemini API error: HTTP {r.status_code}")

    data = r.json()
    candidates = data.get("candidates", [])
    if not candidates:
        raise RuntimeError("Gemini did not return an answer.")

    parts = candidates[0].get("content", {}).get("parts", [])
    answer = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
    if not answer:
        raise RuntimeError("Gemini returned an empty response.")
    return answer[:3900]


def gemini_make_image(prompt):
    if not GEMINI_IMAGE_MODEL:
        raise RuntimeError("Image model is not configured.")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_IMAGE_MODEL}:generateContent"
    )
    r = requests.post(
        url,
        headers={"x-goog-api-key": GEMINI_API_KEY},
        json={
            "contents": [{"parts": [{"text": prompt[:2000]}]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}
        },
        timeout=120
    )
    if r.status_code != 200:
        raise RuntimeError(f"Image API error: HTTP {r.status_code}")

    for part in r.json().get("candidates", [{}])[0].get("content", {}).get("parts", []):
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            return base64.b64decode(inline["data"])
    raise RuntimeError("The configured model did not return an image.")


# ---------------- ACCESS / VIP ----------------

def access_check(tg_id):
    u = get_user(tg_id)
    if not u:
        return False, "Please send /start first."
    if u["banned"]:
        return False, "Your account has been disabled."
    if get_setting("maintenance", "0") == "1" and tg_id != ADMIN_ID:
        return False, "The assistant is temporarily under maintenance."
    if not u["verified"]:
        return False, "Please verify your email first. Send /link."
    return True, ""


def daily_count(tg_id, kind="ai"):
    today = datetime.now(timezone.utc).date().isoformat()
    with connect_db() as con:
        row = con.execute("""
            SELECT COUNT(*) AS n FROM chats
            WHERE telegram_id=? AND role=? AND created_at LIKE ?
        """, (tg_id, kind, today + "%")).fetchone()
    return row["n"]


def check_daily_limit(tg_id, kind="ai"):
    u = get_user(tg_id)
    vip = is_vip(u)
    limit = int(get_setting("vip_daily_limit" if vip else "free_daily_limit", "300" if vip else "30"))
    return daily_count(tg_id, kind) < limit


def save_chat(tg_id, role, message):
    with connect_db() as con:
        con.execute("""
            INSERT INTO chats(telegram_id,role,message,created_at)
            VALUES(?,?,?,?)
        """, (tg_id, role, message[:12000], datetime.now(timezone.utc).isoformat()))


# ---------------- TELEGRAM UI ----------------

def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✦ AI CHAT", callback_data="chat"),
            InlineKeyboardButton("🎨 IMAGE STUDIO", callback_data="image")
        ],
        [
            InlineKeyboardButton("👤 MY PROFILE", callback_data="profile"),
            InlineKeyboardButton("💎 VIP ACCESS", callback_data="vip")
        ],
        [
            InlineKeyboardButton("⚙️ SETTINGS", callback_data="settings"),
            InlineKeyboardButton("❔ HELP", callback_data="help")
        ]
    ])


def profile_text(u):
    vip = is_vip(u)
    expiry = (
        datetime.fromtimestamp(u["vip_until"], timezone.utc).strftime("%d %b %Y")
        if vip else "Not active"
    )
    return (
        "✦ **YOUR ACCOUNT PROFILE**\n\n"
        f"👤 Name: {u['first_name'] or 'User'}\n"
        f"🆔 Telegram ID: `{u['telegram_id']}`\n"
        f"📧 Email: `{u['email'] or 'Not linked'}`\n"
        f"✅ Email verified: {'Yes' if u['verified'] else 'No'}\n"
        f"💎 Membership: {'GOLD VIP' if vip else 'STANDARD'}\n"
        f"📅 VIP expiry: {expiry}\n"
        f"🤖 AI requests: {u['ai_count']}\n"
        f"🎨 Images: {u['image_count']}\n"
        f"🌐 Language: {u['language']}"
    )


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    upsert_telegram_user(tg)
    welcome = get_setting("welcome")
    title = get_setting("menu_title", "✦ GOLDEN AI STUDIO")

    await update.message.reply_text(
        f"👑 **{title}**\n\n{welcome}\n\n"
        "Your personal AI workspace is ready.\n"
        "Verify your email using /link to unlock your account.",
        reply_markup=main_keyboard(),
        parse_mode="Markdown"
    )


async def link_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    u = get_user(tg_id)
    if u and u["verified"]:
        await update.message.reply_text("✅ Your email is already linked.")
        return
    context.user_data["link_state"] = "email"
    await update.message.reply_text("📧 Send your email address to receive a 6-digit OTP. Use /cancel to stop.")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("link_state", None)
    context.user_data.pop("link_email", None)
    await update.message.reply_text("Verification cancelled.")


async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id)
    if not u:
        await update.message.reply_text("Send /start first.")
        return
    await update.message.reply_text(profile_text(u), parse_mode="Markdown", reply_markup=main_keyboard())


async def vip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id)
    if not u:
        await update.message.reply_text("Send /start first.")
        return
    if is_vip(u):
        expiry = datetime.fromtimestamp(u["vip_until"], timezone.utc).strftime("%d %b %Y")
        msg = f"💎 GOLD VIP ACTIVE\nYour membership expires: {expiry}"
    else:
        msg = "✦ STANDARD ACCOUNT\nVIP membership can be activated by the administrator."
    await update.message.reply_text(msg, reply_markup=main_keyboard())


async def image_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    allowed, reason = access_check(tg_id)
    if not allowed:
        await update.message.reply_text(reason)
        return
    if get_setting("image_enabled", "0") != "1":
        await update.message.reply_text("Image Studio is currently unavailable.")
        return
    if not check_daily_limit(tg_id, "image"):
        await update.message.reply_text("Your daily image limit has been reached.")
        return

    prompt = " ".join(context.args).strip()
    if not prompt:
        await update.message.reply_text("Usage: /image describe your image")
        return

    await update.message.reply_text("🎨 Your image is being prepared...")
    try:
        raw = await asyncio.to_thread(gemini_make_image, prompt)
        await update.message.reply_photo(photo=BytesIO(raw), caption="✦ Created by SUPER AI BOT")
        with connect_db() as con:
            con.execute("UPDATE users SET image_count=image_count+1 WHERE telegram_id=?", (tg_id,))
        save_chat(tg_id, "image", prompt)
    except Exception as exc:
        audit("image_error", str(exc), str(tg_id))
        await update.message.reply_text("Image generation failed. Check the configured image model and access.")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tg_id = query.from_user.id
    upsert_telegram_user(query.from_user)
    data = query.data

    if data == "profile":
        u = get_user(tg_id)
        await query.message.reply_text(profile_text(u), parse_mode="Markdown", reply_markup=main_keyboard())
    elif data == "vip":
        u = get_user(tg_id)
        if is_vip(u):
            expiry = datetime.fromtimestamp(u["vip_until"], timezone.utc).strftime("%d %b %Y")
            msg = f"💎 GOLD VIP ACTIVE\nExpiry: {expiry}"
        else:
            msg = "✦ STANDARD MEMBERSHIP\nYour account can be upgraded by the administrator."
        await query.message.reply_text(msg, reply_markup=main_keyboard())
    elif data == "chat":
        await query.message.reply_text("✦ Send your message and Gemini will assist you.")
    elif data == "image":
        await query.message.reply_text("Use /image followed by your image description.")
    elif data == "settings":
        await query.message.reply_text("Account settings:\nUse /profile to view your account.")
    elif data == "help":
        await query.message.reply_text(
            "/start - Main menu\n/link - Verify email\n/profile - My profile\n/vip - Membership\n/image prompt - Image Studio\n/cancel - Cancel linking",
            reply_markup=main_keyboard()
        )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    tg_id = tg.id
    upsert_telegram_user(tg)
    message = (update.message.text or "").strip()

    if get_user(tg_id)["banned"]:
        await update.message.reply_text("Your account is disabled.")
        return

    state = context.user_data.get("link_state")

    if state == "email":
        email = message.lower()
        if not email_valid(email):
            await update.message.reply_text("Please enter a valid email address.")
            return

        with connect_db() as con:
            existing = con.execute(
                "SELECT telegram_id FROM users WHERE email=? AND verified=1",
                (email,)
            ).fetchone()

        if existing and existing["telegram_id"] != tg_id:
            await update.message.reply_text("This email is already linked to another Telegram account.")
            return

        try:
            send_otp("telegram", email)
            context.user_data["link_email"] = email
            context.user_data["link_state"] = "otp"
            await update.message.reply_text("✅ OTP sent to your email. Enter the 6-digit code.")
        except Exception:
            await update.message.reply_text("Email delivery failed. Please try again later.")
        return

    if state == "otp":
        email = context.user_data.get("link_email", "")
        if check_otp("telegram", email, message):
            with connect_db() as con:
                con.execute(
                    "UPDATE users SET email=?,verified=1 WHERE telegram_id=?",
                    (email, tg_id)
                )
            context.user_data.pop("link_state", None)
            context.user_data.pop("link_email", None)
            audit("telegram_email_verified", email, str(tg_id))
            await update.message.reply_text(
                "🎉 Email verified successfully!\nYour personal AI workspace is unlocked.",
                reply_markup=main_keyboard()
            )
        else:
            await update.message.reply_text("Invalid or expired OTP. Use /link to restart.")
        return

    allowed, reason = access_check(tg_id)
    if not allowed:
        await update.message.reply_text(reason)
        return
    if get_setting("ai_enabled", "1") != "1":
        await update.message.reply_text("AI is temporarily disabled.")
        return
    if not check_daily_limit(tg_id, "ai"):
        await update.message.reply_text("Daily AI usage limit reached.")
        return

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    # Simple conversation context: recent messages for this user
    with connect_db() as con:
        history = con.execute(
            "SELECT role,message FROM chats WHERE telegram_id=? ORDER BY id DESC LIMIT 8",
            (tg_id,)
        ).fetchall()

    history_text = "\n".join(
        f"{r['role']}: {r['message']}" for r in reversed(history)
    )
    prompt = f"Recent conversation:\n{history_text}\n\nUser: {message}"

    try:
        answer = await asyncio.to_thread(gemini_generate, prompt)
        save_chat(tg_id, "user", message)
        save_chat(tg_id, "assistant", answer)
        with connect_db() as con:
            con.execute("UPDATE users SET ai_count=ai_count+1 WHERE telegram_id=?", (tg_id,))
        await update.message.reply_text(answer[:3900])
    except Exception as exc:
        audit("gemini_error", str(exc), str(tg_id))
        await update.message.reply_text("The AI service is temporarily unavailable. Please try again.")


# ---------------- WEB UI ----------------

STYLE = """
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--gold:#e7c36d;--gold2:#a98032;--bg:#0a0c14;--card:#151927;--line:#44391f;--muted:#aeb5c6}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(circle at top,#202033,#090b12 55%);color:#f5f2e9;font-family:Inter,Arial,sans-serif}
.wrap{max-width:1100px;margin:28px auto;padding:14px}
.card{background:linear-gradient(145deg,#191e2e,#111521);border:1px solid var(--line);border-radius:18px;padding:20px;margin-bottom:16px;box-shadow:0 12px 35px #0005}
.brand{color:var(--gold);font-weight:900;letter-spacing:2px}
h1,h2,h3{color:var(--gold)}
p{color:#d6d9e2;line-height:1.6}
input,textarea,select{width:100%;padding:12px;margin:7px 0;border-radius:10px;border:1px solid #454a5b;background:#0c101a;color:white}
textarea{min-height:100px}
button,.btn{display:inline-block;background:linear-gradient(120deg,#f0d58c,#ad8135);color:#18130a;border:0;border-radius:10px;padding:12px 17px;font-weight:800;text-decoration:none;cursor:pointer;margin:5px 0}
a{color:var(--gold)}
small,.muted{color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.stat{background:#202536;border:1px solid #403820;border-radius:14px;padding:16px}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:10px;border-bottom:1px solid #34394a;text-align:left;overflow-wrap:anywhere}
label{display:block;color:#e8d7ad;margin-top:8px}
</style>
"""


def render_page(title, content):
    return render_template_string(
        """<!doctype html><html><head><title>{{title}}</title>""" + STYLE +
        """</head><body><div class="wrap">
        <div class="card"><div class="brand">👑 SUPER AI BOT V3</div>
        <small>GOLDEN ELITE CONTROL CENTER</small></div>
        {{content|safe}}</div></body></html>""",
        title=title, content=content
    )


def csrf():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def verify_csrf():
    value = request.form.get("csrf", "")
    expected = session.get("csrf", "")
    if not expected or not hmac.compare_digest(value, expected):
        abort(400, "Invalid security token")


def admin_only(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect("/admin")
        return fn(*args, **kwargs)
    return wrapper


@app.get("/")
def home():
    if session.get("web_email"):
        return redirect("/profile")
    token = html.escape(csrf())
    content = f"""
    <div class="card">
      <h1>Welcome to your AI workspace</h1>
      <p>Sign in with your email to access your personal account.</p>
      <form method="post" action="/send-otp">
        <input type="hidden" name="csrf" value="{token}">
        <input type="email" name="email" placeholder="Your email address" required>
        <button>Send verification code</button>
      </form>
    </div>"""
    return render_page("Login", content)


@app.post("/send-otp")
def web_send_otp():
    verify_csrf()
    email = request.form.get("email", "").strip().lower()
    if not email_valid(email):
        abort(400, "Invalid email")

    try:
        send_otp("website", email)
    except Exception:
        return render_page("Email error", "<div class='card'><h2>Email delivery failed</h2><a href='/'>Try again</a></div>"), 400

    session["pending_email"] = email
    token = html.escape(csrf())
    content = f"""
    <div class="card"><h2>Verify your email</h2>
    <form method="post" action="/verify-otp">
      <input type="hidden" name="csrf" value="{token}">
      <input name="otp" maxlength="6" inputmode="numeric" placeholder="6-digit code" required>
      <button>Verify account</button>
    </form></div>"""
    return render_page("Verify", content)


@app.post("/verify-otp")
def web_verify_otp():
    verify_csrf()
    email = session.get("pending_email")
    code = request.form.get("otp", "").strip()
    if not email or not check_otp("website", email, code):
        return render_page("Invalid code", "<div class='card'><h2>Invalid or expired code</h2><a href='/'>Try again</a></div>"), 400

    with connect_db() as con:
        con.execute("""
            INSERT INTO web_users(email,verified,created_at)
            VALUES(?,1,?)
            ON CONFLICT(email) DO UPDATE SET verified=1
        """, (email, datetime.now(timezone.utc).isoformat()))

    session.pop("pending_email", None)
    session["web_email"] = email
    session.permanent = True
    return redirect("/profile")


@app.get("/profile")
def web_profile():
    email = session.get("web_email")
    if not email:
        return redirect("/")
    safe = html.escape(email)
    content = f"""
    <div class="card"><h2>My Account</h2>
    <p>📧 Verified email: {safe}</p>
    <p>Connect Telegram by opening your bot and sending <b>/link</b>.</p>
    <p>Your Telegram profile and email are linked after OTP verification.</p>
    <a class="btn" href="/logout">Logout</a></div>"""
    return render_page("My Account", content)


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/")


# ---------------- ADMIN ----------------

@app.get("/admin")
def admin_login():
    if session.get("admin"):
        return redirect("/admin/dashboard")
    token = html.escape(csrf())
    content = f"""
    <div class="card"><h2>👑 Owner Login</h2>
    <form method="post" action="/admin/login">
      <input type="hidden" name="csrf" value="{token}">
      <input type="password" name="password" placeholder="Owner password" required>
      <button>Enter Control Center</button>
    </form></div>"""
    return render_page("Admin Login", content)


@app.post("/admin/login")
def admin_login_post():
    verify_csrf()
    password = request.form.get("password", "")
    if not ADMIN_PASSWORD or not hmac.compare_digest(password, ADMIN_PASSWORD):
        return render_page("Login failed", "<div class='card'><h2>Incorrect password</h2><a href='/admin'>Try again</a></div>"), 401
    session.clear()
    session["admin"] = True
    session.permanent = True
    audit("admin_login", "Successful login")
    return redirect("/admin/dashboard")


@app.get("/admin/dashboard")
@admin_only
def admin_dashboard():
    with connect_db() as con:
        total = con.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        verified = con.execute("SELECT COUNT(*) n FROM users WHERE verified=1").fetchone()["n"]
        banned = con.execute("SELECT COUNT(*) n FROM users WHERE banned=1").fetchone()["n"]
        vip = con.execute("SELECT COUNT(*) n FROM users WHERE vip_until>?", (int(time.time()),)).fetchone()["n"]
        users = con.execute(
            "SELECT telegram_id,email,first_name,verified,banned,vip_plan,vip_until FROM users ORDER BY joined_at DESC LIMIT 100"
        ).fetchall()
        logs = con.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 10").fetchall()

    rows = ""
    for u in users:
        rows += (
            "<tr><td>" + str(u["telegram_id"]) + "</td><td>" +
            html.escape(u["first_name"] or "") + "</td><td>" +
            html.escape(u["email"] or "-") + "</td><td>" +
            ("Yes" if u["verified"] else "No") + "</td><td>" +
            ("Yes" if u["banned"] else "No") + "</td><td>" +
            html.escape(u["vip_plan"] or "FREE") + "</td></tr>"
        )

    log_rows = "".join(
        "<tr><td>" + html.escape(x["created_at"]) + "</td><td>" +
        html.escape(x["action"]) + "</td><td>" +
        html.escape(x["details"]) + "</td></tr>" for x in logs
    )

    token = html.escape(csrf())
    content = f"""
    <div class="grid">
      <div class="stat"><small>Total users</small><h2>{total}</h2></div>
      <div class="stat"><small>Verified</small><h2>{verified}</h2></div>
      <div class="stat"><small>VIP members</small><h2>{vip}</h2></div>
      <div class="stat"><small>Banned</small><h2>{banned}</h2></div>
    </div>

    <div class="card"><h2>✦ Bot Configuration</h2>
      <form method="post" action="/admin/settings">
        <input type="hidden" name="csrf" value="{token}">
        <label>Welcome message</label>
        <textarea name="welcome">{html.escape(get_setting("welcome"))}</textarea>
        <label>AI personality / system instructions</label>
        <textarea name="ai_personality">{html.escape(get_setting("ai_personality"))}</textarea>
        <label>Menu title</label>
        <input name="menu_title" value="{html.escape(get_setting("menu_title"))}">
        <label>Free daily AI limit</label>
        <input type="number" min="1" max="1000" name="free_daily_limit" value="{html.escape(get_setting("free_daily_limit"))}">
        <label>VIP daily AI limit</label>
        <input type="number" min="1" max="5000" name="vip_daily_limit" value="{html.escape(get_setting("vip_daily_limit"))}">
        <label><input style="width:auto" type="checkbox" name="ai_enabled" {"checked" if get_setting("ai_enabled")=="1" else ""}> AI enabled</label>
        <label><input style="width:auto" type="checkbox" name="image_enabled" {"checked" if get_setting("image_enabled")=="1" else ""}> Image generation enabled</label>
        <label><input style="width:auto" type="checkbox" name="maintenance" {"checked" if get_setting("maintenance")=="1" else ""}> Maintenance mode</label>
        <button>Save bot settings</button>
      </form>
    </div>

    <div class="card"><h2>💎 VIP Membership</h2>
      <form method="post" action="/admin/vip">
        <input type="hidden" name="csrf" value="{token}">
        <input type="number" name="telegram_id" placeholder="Telegram ID" required>
        <select name="plan">
          <option value="7">7 Days</option>
          <option value="30">30 Days</option>
          <option value="90">90 Days</option>
          <option value="36500">Lifetime</option>
          <option value="0">Remove VIP</option>
        </select>
        <button>Update membership</button>
      </form>
    </div>

    <div class="card"><h2>🛡 User Access</h2>
      <form method="post" action="/admin/ban">
        <input type="hidden" name="csrf" value="{token}">
        <input type="number" name="telegram_id" placeholder="Telegram ID" required>
        <select name="action"><option value="ban">Ban</option><option value="unban">Unban</option></select>
        <button>Apply</button>
      </form>
    </div>

    <div class="card"><h2>📢 Broadcast</h2>
      <form method="post" action="/admin/broadcast">
        <input type="hidden" name="csrf" value="{token}">
        <textarea name="message" placeholder="Write broadcast message" required></textarea>
        <button>Send to opted-in verified users</button>
      </form>
    </div>

    <div class="card"><h2>✉ Individual Message</h2>
      <form method="post" action="/admin/message">
        <input type="hidden" name="csrf" value="{token}">
        <input type="number" name="telegram_id" placeholder="Telegram ID" required>
        <textarea name="message" placeholder="Message" required></textarea>
        <button>Send message</button>
      </form>
    </div>

    <div class="card"><h2>👥 User Directory</h2>
      <div style="overflow:auto"><table>
      <tr><th>ID</th><th>Name</th><th>Email</th><th>Verified</th><th>Banned</th><th>Plan</th></tr>
      {rows}</table></div>
    </div>

    <div class="card"><h2>📜 Recent Admin Activity</h2>
      <div style="overflow:auto"><table>
      <tr><th>Date</th><th>Action</th><th>Details</th></tr>
      {log_rows}</table></div>
      <a href="/admin/logout">Logout</a>
    </div>
    """
    return render_page("Golden Admin", content)


@app.post("/admin/settings")
@admin_only
def admin_settings():
    verify_csrf()
    allowed = [
        "welcome", "ai_personality", "menu_title",
        "free_daily_limit", "vip_daily_limit"
    ]
    for key in allowed:
        value = request.form.get(key, "").strip()
        if key.endswith("limit"):
            try:
                n = int(value)
                if n < 1 or n > 5000:
                    abort(400, "Invalid usage limit")
            except ValueError:
                abort(400, "Invalid usage limit")
        set_setting(key, value[:8000])

    set_setting("ai_enabled", "1" if request.form.get("ai_enabled") else "0")
    set_setting("image_enabled", "1" if request.form.get("image_enabled") else "0")
    set_setting("maintenance", "1" if request.form.get("maintenance") else "0")
    audit("settings_updated", "Bot settings updated")
    return redirect("/admin/dashboard")


@app.post("/admin/vip")
@admin_only
def admin_vip():
    verify_csrf()
    try:
        tg_id = int(request.form.get("telegram_id", "0"))
        days = int(request.form.get("plan", "0"))
    except ValueError:
        abort(400)

    if tg_id <= 0 or tg_id == ADMIN_ID or days not in (0, 7, 30, 90, 36500):
        abort(400)

    expiry = 0 if days == 0 else (
        int(time.time()) + days * 86400 if days != 36500 else 4102444800
    )

    with connect_db() as con:
        if days == 0:
            con.execute(
                "UPDATE users SET vip_until=0,vip_plan='FREE' WHERE telegram_id=?",
                (tg_id,)
            )
        else:
            con.execute(
                "UPDATE users SET vip_until=?,vip_plan=? WHERE telegram_id=?",
                (expiry, "LIFETIME" if days == 36500 else f"{days} DAYS", tg_id)
            )

    audit("vip_updated", f"id={tg_id}, days={days}")
    return redirect("/admin/dashboard")


@app.post("/admin/ban")
@admin_only
def admin_ban():
    verify_csrf()
    try:
        tg_id = int(request.form.get("telegram_id", "0"))
    except ValueError:
        abort(400)
    action = request.form.get("action")
    if tg_id <= 0 or tg_id == ADMIN_ID or action not in ("ban", "unban"):
        abort(400)
    with connect_db() as con:
        con.execute(
            "UPDATE users SET banned=? WHERE telegram_id=?",
            (1 if action == "ban" else 0, tg_id)
        )
    audit("user_" + action, str(tg_id))
    return redirect("/admin/dashboard")


def send_telegram_sync(tg_id, message):
    if not telegram_loop or not telegram_app or not telegram_ready:
        raise RuntimeError("Telegram bot is not ready")
    future = asyncio.run_coroutine_threadsafe(
        telegram_app.bot.send_message(chat_id=tg_id, text=message[:4000]),
        telegram_loop
    )
    return future.result(timeout=30)


@app.post("/admin/message")
@admin_only
def admin_message():
    verify_csrf()
    try:
        tg_id = int(request.form.get("telegram_id", "0"))
    except ValueError:
        abort(400)
    message = request.form.get("message", "").strip()
    if tg_id <= 0 or not message:
        abort(400)
    try:
        send_telegram_sync(tg_id, message)
        result = "Message sent successfully."
    except Exception:
        result = "Message could not be delivered."
    audit("individual_message", f"id={tg_id}: {result}")
    return render_page("Message", f"<div class='card'><h2>{html.escape(result)}</h2><a href='/admin/dashboard'>Back</a></div>")


def broadcast_worker(message):
    with connect_db() as con:
        rows = con.execute("""
            SELECT telegram_id FROM users
            WHERE verified=1 AND opted_in=1 AND banned=0
        """).fetchall()
    sent, failed = 0, 0
    for row in rows:
        try:
            send_telegram_sync(row["telegram_id"], message)
            sent += 1
        except Exception:
            failed += 1
        time.sleep(0.07)
    audit("broadcast_finished", f"sent={sent}; failed={failed}")


@app.post("/admin/broadcast")
@admin_only
def admin_broadcast():
    verify_csrf()
    message = request.form.get("message", "").strip()
    if not message:
        abort(400)
    threading.Thread(target=broadcast_worker, args=(message,), daemon=True).start()
    audit("broadcast_started", "Admin started broadcast")
    return render_page("Broadcast", "<div class='card'><h2>Broadcast started</h2><p>Delivery results will appear in the activity log.</p><a href='/admin/dashboard'>Back</a></div>")


@app.get("/admin/logout")
def admin_logout():
    session.clear()
    return redirect("/admin")


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "telegram_ready": telegram_ready,
        "ai_enabled": get_setting("ai_enabled") == "1",
        "image_enabled": get_setting("image_enabled") == "1"
    })


# ---------------- STARTUP ----------------

async def telegram_post_init(application):
    global telegram_loop, telegram_ready
    telegram_loop = asyncio.get_running_loop()
    telegram_ready = True
    audit("telegram_started", "Polling active", "SYSTEM")


def run_telegram():
    global telegram_app, startup_error

    telegram_app = Application.builder().token(BOT_TOKEN).post_init(telegram_post_init).build()
    telegram_app.add_handler(CommandHandler("start", start_cmd))
    telegram_app.add_handler(CommandHandler("link", link_cmd))
    telegram_app.add_handler(CommandHandler("cancel", cancel_cmd))
    telegram_app.add_handler(CommandHandler("profile", profile_cmd))
    telegram_app.add_handler(CommandHandler("vip", vip_cmd))
    telegram_app.add_handler(CommandHandler("image", image_cmd))
    telegram_app.add_handler(CallbackQueryHandler(button_handler))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    telegram_app.run_polling(drop_pending_updates=True)


def main():
    global startup_error
    init_db()

    required = {
        "BOT_TOKEN": BOT_TOKEN,
        "ADMIN_PANEL_PASSWORD": ADMIN_PASSWORD,
        "BREVO_API_KEY": BREVO_API_KEY,
        "SENDER_EMAIL": SENDER_EMAIL,
        "GEMINI_API_KEY": GEMINI_API_KEY
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        startup_error = "Missing environment variables: " + ", ".join(missing)
        raise RuntimeError(startup_error)

    if ADMIN_ID <= 0:
        raise RuntimeError("ADMIN_ID must be a valid Telegram numeric ID.")

    threading.Thread(target=run_telegram, daemon=True).start()
    serve(app, host="0.0.0.0", port=PORT, threads=8)


if __name__ == "__main__":
    main()
