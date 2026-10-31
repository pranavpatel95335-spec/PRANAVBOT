# SUPER AI BOT V3

## Features
- Telegram AI text chat (free public text endpoint; provider availability/rate limits may vary)
- Image generation command `/image <prompt>` (free public image endpoint; availability/rate limits may vary)
- `/start`, `/help`, `/id`, `/admin`
- SQLite user tracking, last activity, message count, ban/unban
- Web email OTP portal (Gmail SMTP)
- Admin dashboard for AI/image/maintenance toggles and user management

## Render setup
1. Create a Render Web Service from this repository.
2. Runtime: Python; Build: `pip install -r requirements.txt`; Start: `python main.py`.
3. Add environment variables:
   - `BOT_TOKEN`: Telegram bot token from BotFather
   - `ADMIN_ID`: your numeric Telegram ID (default in render.yaml is the ID supplied by the owner)
   - `ADMIN_PANEL_PASSWORD`: a new strong password for the web admin panel
   - `SECRET_KEY`: random long secret (render.yaml can generate it)
   - `SMTP_EMAIL`: dedicated Gmail sender address
   - `SMTP_APP_PASSWORD`: Gmail App Password (never put it in GitHub or chat)
4. Open the Render service URL. `/admin` is the admin panel; `/login` is the email OTP portal.
5. In Telegram, open the bot and send `/start`.

## Gmail OTP
Use a dedicated sender mailbox where possible. Gmail SMTP generally requires 2-Step Verification and an App Password; eligibility and sending limits are controlled by Google. Never share the App Password. Put it only in Render Environment Variables.

## Important limitations
- Email OTP signs users into the web portal; Telegram bot users are identified by Telegram ID. Telegram does not expose a user's Gmail or phone number automatically.
- Phone numbers are not collected. Email addresses appear in the admin dashboard only after the user voluntarily verifies them on the web portal.
- Free AI/image endpoints are third-party public services and may change, rate-limit, or stop working. This project does not promise unlimited generation or a specific commercial AI model.
- Render free services may sleep/restart. Their local filesystem is ephemeral; SQLite data may be lost on redeploy/restart. Use a persistent database/storage service for durable production data.
- Admin dashboard currently supports feature toggles and user ban/unban. It does not expose passwords, OTP values, or SMTP secrets.
