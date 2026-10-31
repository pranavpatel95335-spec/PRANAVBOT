import os
import re
import time
import hmac
import hashlib
import secrets
import sqlite3
import threading
import logging
from datetime import datetime, timezone
from urllib.parse import quote
from email.message import EmailMessage
import smtplib

import requests
from flask import Flask, request, redirect, url_for, session, render_template_string, flash
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ---------------- Configuration ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "8585055903"))
ADMIN_PANEL_PASSWORD = os.getenv("ADMIN_PANEL_PASSWORD", "")
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))
DB_PATH = os.getenv("DB_PATH", "super_ai_bot.db")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_EMAIL = os.getenv("SMTP_EMAIL", "")
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD", "")
OTP_TTL_SECONDS = 300
OTP_RESEND_SECONDS = 60
MAX_OTP_ATTEMPTS = 5
TEXT_API_BASE = os.getenv("TEXT_API_BASE", "https://text.pollinations.ai")
IMAGE_API_BASE = os.getenv("IMAGE_API_BASE", "https://image.pollinations.ai/prompt")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("super-ai-bot-v3")

web = Flask(__name__)
web.secret_key = SECRET_KEY
web.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

# ---------------- Database ----------------
def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            telegram_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            last_name TEXT, language_code TEXT, created_at TEXT, last_seen TEXT,
            messages INTEGER DEFAULT 0, is_banned INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS web_users(
            email TEXT PRIMARY KEY, created_at TEXT, last_login TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS otps(
            email TEXT PRIMARY KEY, otp_hash TEXT, expires_at INTEGER,
            sent_at INTEGER, attempts INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
        defaults = {
            "ai_enabled":"1", "image_enabled":"1", "maintenance":"0",
            "welcome_text":"Welcome to SUPER AI BOT V3!"
        }
        for k, v in defaults.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k,v))

def setting(key, default="0"):
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

def set_setting(key, value):
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

def touch_user(user):
    if not user:
        return
    now = datetime.now(timezone.utc).isoformat()
    with db() as c:
        c.execute("""INSERT INTO users(telegram_id,username,first_name,last_name,language_code,created_at,last_seen)
        VALUES(?,?,?,?,?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET
        username=excluded.username, first_name=excluded.first_name, last_name=excluded.last_name,
        language_code=excluded.language_code,last_seen=excluded.last_seen""",
        (user.id, user.username, user.first_name, user.last_name, user.language_code, now, now))

def is_banned(uid):
    with db() as c:
        row = c.execute("SELECT is_banned FROM users WHERE telegram_id=?", (uid,)).fetchone()
        return bool(row and row["is_banned"])

def count_message(uid):
    with db() as c:
        c.execute("UPDATE users SET messages=messages+1 WHERE telegram_id=?", (uid,))

def admin_only(uid):
    return uid == ADMIN_ID

# ---------------- AI helpers ----------------
def ask_ai(prompt):
    # Free public endpoint; availability/rate limits are controlled by the provider.
    url = TEXT_API_BASE.rstrip("/") + "/" + quote(prompt[:5000], safe="")
    r = requests.get(url, timeout=90, headers={"User-Agent":"SUPER-AI-BOT-V3"})
    r.raise_for_status()
    answer = r.text.strip()
    if not answer:
        raise RuntimeError("AI provider returned an empty response")
    return answer[:3900]

def image_url(prompt):
    return IMAGE_API_BASE.rstrip("/") + "/" + quote(prompt[:500], safe="") + "?width=1024&height=1024&nologo=true"

# ---------------- Telegram bot ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    touch_user(user)
    if is_banned(user.id):
        await update.effective_message.reply_text("Your access is disabled by the administrator.")
        return
    await update.effective_message.reply_text(
        "🤖 *SUPER AI BOT V3*\\n\\n"
        "Send a message to chat with AI.\\n"
        "/image followed by a prompt — generate an image\\n"
        "/help — commands\\n"
        "/id — your Telegram ID",
        parse_mode="Markdown"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user(update.effective_user)
    await update.effective_message.reply_text(
        "Commands:\\n/start — start bot\\n/help — help\\n/id — show your ID\\n"
        "/image prompt — generate an image\\n"
        "Send normal text for AI chat."
    )

async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user(update.effective_user)
    await update.effective_message.reply_text(f"Your Telegram ID: {update.effective_user.id}")

async def image_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user(update.effective_user)
    if is_banned(update.effective_user.id):
        return
    if setting("maintenance") == "1" and not admin_only(update.effective_user.id):
        await update.effective_message.reply_text("Bot is temporarily under maintenance.")
        return
    if setting("image_enabled") != "1":
        await update.effective_message.reply_text("Image generation is currently disabled.")
        return
    prompt = " ".join(context.args).strip()
    if not prompt:
        await update.effective_message.reply_text("Usage: /image a small house beside a lake")
        return
    msg = await update.effective_message.reply_text("🎨 Creating your image…")
    try:
        url = image_url(prompt)
        response = requests.get(url, timeout=120, stream=True)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "image" not in content_type:
            raise RuntimeError("Image provider did not return an image.")
        data = response.content
        if len(data) > 9_500_000:
            raise RuntimeError("Generated image is too large to send.")
        await update.effective_message.reply_photo(photo=data, caption="Generated by SUPER AI BOT V3")
        await msg.delete()
    except Exception as e:
        log.exception("Image generation failed")
        await msg.edit_text("Image service is temporarily unavailable. Please try again later.")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    touch_user(user)
    if is_banned(user.id):
        await update.effective_message.reply_text("Your access is disabled by the administrator.")
        return
    if setting("maintenance") == "1" and not admin_only(user.id):
        await update.effective_message.reply_text("Bot is temporarily under maintenance.")
        return
    if setting("ai_enabled") != "1":
        await update.effective_message.reply_text("AI chat is currently disabled.")
        return
    prompt = (update.effective_message.text or "").strip()
    if not prompt:
        return
    count_message(user.id)
    wait = await update.effective_message.reply_text("🤔 Thinking…")
    try:
        answer = await asyncio_to_thread(ask_ai, prompt)
        await wait.edit_text(answer)
    except Exception:
        log.exception("Text AI request failed")
        await wait.edit_text("AI service is busy or unavailable right now. Please try again later.")

async def asyncio_to_thread(fn, *args):
    import asyncio
    return await asyncio.to_thread(fn, *args)

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_user(update.effective_user)
    if not admin_only(update.effective_user.id):
        await update.effective_message.reply_text("Not authorized.")
        return
    with db() as c:
        total = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        banned = c.execute("SELECT COUNT(*) n FROM users WHERE is_banned=1").fetchone()["n"]
    await update.effective_message.reply_text(
        f"🛠 SUPER AI BOT V3 Admin\\nUsers: {total}\\nBanned: {banned}\\n"
        f"AI: {setting('ai_enabled')} | Images: {setting('image_enabled')} | Maintenance: {setting('maintenance')}\\n"
        "Open the web admin panel URL configured in Render to manage settings."
    )

def run_bot():
    if not BOT_TOKEN:
        log.error("BOT_TOKEN is missing; Telegram bot will not start.")
        return
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("id", id_cmd))
    application.add_handler(CommandHandler("image", image_cmd))
    application.add_handler(CommandHandler("admin", admin_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    log.info("SUPER AI BOT V3 polling started")
    application.run_polling(drop_pending_updates=True)

# ---------------- Web UI ----------------
PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{title}}</title><style>
*{box-sizing:border-box}body{margin:0;background:#080d1c;color:#eef2ff;font:15px system-ui;padding:20px}
main{max-width:900px;margin:auto}.card{background:#111a31;border:1px solid #263653;border-radius:18px;padding:18px;margin:12px 0}
h1{font-size:24px}h2{font-size:18px}input,button{padding:12px;border-radius:10px;border:1px solid #344565;background:#0b1327;color:white;width:100%;margin:6px 0}
button{background:#7047eb;border:0;font-weight:700}a{color:#b8a5ff}.muted{color:#aab6d0}.row{display:flex;gap:12px;flex-wrap:wrap}.row>*{flex:1;min-width:140px}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:9px;border-bottom:1px solid #2a3855;overflow-wrap:anywhere}
.ok{color:#7de5ac}.err{color:#ff9b9b}
</style></head><body><main><h1>✨ SUPER AI BOT V3</h1>{% with messages=get_flashed_messages() %}{% for m in messages %}<div class="card">{{m}}</div>{% endfor %}{% endwith %}{{body|safe}}</main></body></html>"""

def page(body, title="SUPER AI BOT V3"):
    return render_template_string(PAGE, body=body, title=title)

@web.route("/")
def home():
    body = """<div class="card"><h2>🤖 SUPER AI BOT V3</h2><p>Telegram AI assistant and account portal.</p>
    <p><a href="/login">Email OTP login</a> · <a href="/admin">Admin panel</a></p></div>"""
    return page(body)

@web.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email","").strip().lower()
        if not re.fullmatch(r"[^@\\s]+@[^@\\s]+\\.[^@\\s]+", email) or len(email) > 254:
            flash("Enter a valid email address.")
            return redirect(url_for("login"))
        if not SMTP_EMAIL or not SMTP_APP_PASSWORD:
            flash("Email OTP is not configured yet. Admin must set SMTP_EMAIL and SMTP_APP_PASSWORD in Render.")
            return redirect(url_for("login"))
        now = int(time.time())
        with db() as c:
            old = c.execute("SELECT sent_at FROM otps WHERE email=?", (email,)).fetchone()
            if old and now - old["sent_at"] < OTP_RESEND_SECONDS:
                flash("Please wait before requesting another code.")
                return redirect(url_for("login"))
        otp = f"{secrets.randbelow(1000000):06d}"
        otp_hash = hashlib.sha256((otp + SECRET_KEY).encode()).hexdigest()
        with db() as c:
            c.execute("""INSERT INTO otps(email,otp_hash,expires_at,sent_at,attempts) VALUES(?,?,?,?,0)
            ON CONFLICT(email) DO UPDATE SET otp_hash=excluded.otp_hash,expires_at=excluded.expires_at,
            sent_at=excluded.sent_at,attempts=0""", (email, otp_hash, now+OTP_TTL_SECONDS, now))
        try:
            mail = EmailMessage()
            mail["Subject"] = "Your SUPER AI BOT V3 verification code"
            mail["From"] = SMTP_EMAIL
            mail["To"] = email
            mail.set_content(f"Your verification code is {otp}. It expires in 5 minutes. If you did not request it, ignore this email.")
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
                server.starttls()
                server.login(SMTP_EMAIL, SMTP_APP_PASSWORD)
                server.send_message(mail)
            session["pending_email"] = email
            flash("OTP sent. Check your inbox.")
            return redirect(url_for("verify"))
        except Exception:
            log.exception("SMTP send failed")
            flash("Could not send OTP. Check SMTP settings in Render.")
            return redirect(url_for("login"))
    body = """<div class="card"><h2>Email OTP Login</h2><form method="post"><label>Email address</label>
    <input name="email" type="email" required maxlength="254" placeholder="you@example.com"><button>Send OTP</button></form>
    <p class="muted">A one-time code will be sent to this email. No phone number is required.</p></div>"""
    return page(body)

@web.route("/verify", methods=["GET","POST"])
def verify():
    email = session.get("pending_email")
    if not email:
        return redirect(url_for("login"))
    if request.method == "POST":
        code = request.form.get("otp","").strip()
        now = int(time.time())
        with db() as c:
            row = c.execute("SELECT * FROM otps WHERE email=?", (email,)).fetchone()
            if not row or row["expires_at"] < now or row["attempts"] >= MAX_OTP_ATTEMPTS:
                c.execute("DELETE FROM otps WHERE email=?", (email,))
                flash("Code expired or attempt limit reached. Request a new one.")
                session.pop("pending_email", None)
                return redirect(url_for("login"))
            expected = hashlib.sha256((code + SECRET_KEY).encode()).hexdigest()
            if not hmac.compare_digest(expected, row["otp_hash"]):
                c.execute("UPDATE otps SET attempts=attempts+1 WHERE email=?", (email,))
                flash("Incorrect code.")
                return redirect(url_for("verify"))
            c.execute("DELETE FROM otps WHERE email=?", (email,))
            c.execute("""INSERT INTO web_users(email,created_at,last_login) VALUES(?,?,?)
            ON CONFLICT(email) DO UPDATE SET last_login=excluded.last_login""",
            (email, datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()))
        session.pop("pending_email", None)
        session["web_email"] = email
        return redirect(url_for("account"))
    body = """<div class="card"><h2>Verify email</h2><form method="post"><input name="otp" inputmode="numeric" pattern="[0-9]{6}" minlength="6" maxlength="6" required placeholder="6-digit OTP"><button>Verify & sign in</button></form></div>"""
    return page(body)

@web.route("/account")
def account():
    email = session.get("web_email")
    if not email:
        return redirect(url_for("login"))
    body = f"""<div class="card"><h2>Signed in</h2><p>Verified email: <b>{email}</b></p>
    <p class="muted">This email login is for the web portal. Telegram bot identity remains your Telegram account.</p>
    <a href="/logout">Log out</a></div>"""
    return page(body)

@web.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

def admin_auth():
    return bool(ADMIN_PANEL_PASSWORD and session.get("admin_ok") is True)

@web.route("/admin", methods=["GET","POST"])
def admin():
    if not admin_auth():
        if request.method == "POST":
            password = request.form.get("password","")
            if ADMIN_PANEL_PASSWORD and hmac.compare_digest(password, ADMIN_PANEL_PASSWORD):
                session["admin_ok"] = True
                return redirect(url_for("admin"))
            flash("Incorrect password or admin password is not configured.")
        body = """<div class="card"><h2>Admin Login</h2><form method="post"><input type="password" name="password" required placeholder="Admin panel password"><button>Sign in</button></form></div>"""
        return page(body)
    with db() as c:
        users = c.execute("SELECT * FROM users ORDER BY last_seen DESC LIMIT 200").fetchall()
        web_users = c.execute("SELECT * FROM web_users ORDER BY last_login DESC LIMIT 200").fetchall()
    controls = ""
    for key, label in [("ai_enabled","AI chat"),("image_enabled","Image generation"),("maintenance","Maintenance mode")]:
        checked = "checked" if setting(key) == "1" else ""
        controls += f'<label><input style="width:auto" type="checkbox" name="{key}" value="1" {checked}> {label}</label><br>'
    rows = "".join(f"<tr><td>{u['telegram_id']}</td><td>{u['first_name'] or ''}</td><td>@{u['username'] or ''}</td><td>{u['messages']}</td><td>{u['last_seen'] or ''}</td><td>{'BANNED' if u['is_banned'] else 'Active'}</td><td><form method='post' action='/admin/ban'><input type='hidden' name='uid' value='{u['telegram_id']}'><button>{'Unban' if u['is_banned'] else 'Ban'}</button></form></td></tr>" for u in users)
    emails = "".join(f"<tr><td>{u['email']}</td><td>{u['created_at']}</td><td>{u['last_login']}</td></tr>" for u in web_users)
    body = f"""<div class="card"><h2>Controls</h2><form method="post" action="/admin/settings">{controls}<button>Save settings</button></form></div>
    <div class="card"><h2>Telegram users (latest 200)</h2><div style="overflow:auto"><table><tr><th>ID</th><th>Name</th><th>Username</th><th>Messages</th><th>Last seen</th><th>Status</th><th>Action</th></tr>{rows}</table></div></div>
    <div class="card"><h2>Verified web emails (latest 200)</h2><div style="overflow:auto"><table><tr><th>Email</th><th>Created</th><th>Last login</th></tr>{emails}</table></div></div>
    <div class="card"><a href="/admin/logout">Admin logout</a></div>"""
    return page(body)

@web.route("/admin/settings", methods=["POST"])
def admin_settings():
    if not admin_auth(): return redirect(url_for("admin"))
    for key in ["ai_enabled","image_enabled","maintenance"]:
        set_setting(key, "1" if request.form.get(key) == "1" else "0")
    flash("Settings saved.")
    return redirect(url_for("admin"))

@web.route("/admin/ban", methods=["POST"])
def admin_ban():
    if not admin_auth(): return redirect(url_for("admin"))
    try:
        uid = int(request.form.get("uid",""))
        with db() as c:
            row = c.execute("SELECT is_banned FROM users WHERE telegram_id=?", (uid,)).fetchone()
            if row:
                c.execute("UPDATE users SET is_banned=? WHERE telegram_id=?", (0 if row["is_banned"] else 1, uid))
    except ValueError:
        pass
    return redirect(url_for("admin"))

@web.route("/admin/logout")
def admin_logout():
    session.pop("admin_ok", None)
    return redirect(url_for("admin"))

def run_web():
    port = int(os.getenv("PORT", "10000"))
    web.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    init_db()
    if not ADMIN_PANEL_PASSWORD:
        log.warning("ADMIN_PANEL_PASSWORD is not set; web admin login is disabled.")
    threading.Thread(target=run_web, daemon=True).start()
    run_bot()
