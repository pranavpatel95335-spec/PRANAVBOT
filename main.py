import os
import zipfile
import shutil
import threading
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters

# 1. Render ko 24/7 zinda rakhne ke liye Flask Web Server (Keep-Alive)
app_web = Flask('')

@app_web.route('/')
def home():
    return "🤖 Pranav Modz Patcher Bot is 24/7 Alive and Running!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app_web.run(host='0.0.0.0', port=port)

def keep_alive():
    t = threading.Thread(target=run_web)
    t.daemon = True
    t.start()


# 2. Telegram Bot & Auto-Patching Engine
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Jab user /start likhega, tab ye welcome message bhejega.
    """
    await update.message.reply_text(
        "👋 स्वागत है प्रणव जी के APK पैचर बोट में!\n\n"
        "⚡ अपनी .apk फाइल यहाँ भेजो, बोट उसके अंदर ऑटोमैटिक .dex फाइल और हुक सेट करके तुम्हें वापस दे देगा!"
    )

def patch_apk(input_apk, output_apk):
    """
    Ye function bina kisi heavy tool ke, seedhe Python ke zipfile module se 
    APK ke andar teri .dex file (classes9.dex) ko safely inject kar deta hai.
    """
    try:
        # Input APK ki copy banao output path par
        shutil.copy(input_apk, output_apk)
        dex_filename = "classes9.dex"
        
        # APK ke andar dex file zip ki tarah add kar do
        with zipfile.ZipFile(output_apk, 'a') as zip_ref:
            if os.path.exists(dex_filename):
                zip_ref.write(dex_filename, arcname=dex_filename)
                print(f"[PRANAV MODZ] Successfully injected {dex_filename} into APK!")
            else:
                print(f"[WARNING] {dex_filename} root folder mein nahi mili, par APK copy ho gayi hai.")
                
        return True
    except Exception as e:
        print(f"[ERROR] Patching failed: {str(e)}")
        return False

async def handle_apk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    
    # Check karega ki user ne .apk file bheji hai ya nahi
    if message.document and message.document.file_name.endswith('.apk'):
        await message.reply_text("⚡ भाई, APK मिल गई है! बोट इसके अंदर ऑटोमैटिक .dex फाइल और हुक सेट कर रहा है, एक सेकंड रुक...")
        
        file = await message.document.get_file()
        input_path = f"input_{message.from_user.id}.apk"
        output_path = f"pranav_modded_{message.from_user.id}.apk"
        
        try:
            # APK download karega
            await file.download_to_drive(input_path)
            
            # Auto-patching function call hoga
            success = patch_apk(input_path, output_path)
            
            if success and os.path.exists(output_path):
                # Mod ki hui APK user ko wapas bhej dega
                await message.reply_document(
                    document=open(output_path, 'rb'),
                    caption="✅ ले भाई! तेरी APK पूरी तरह मॉड होकर तैयार है। अब इसे इंस्टॉल कर ले!"
                )
            else:
                await message.reply_text("❌ भाई, APK पैच करने में कुछ लोचा हो गया।")
                
        except Exception as e:
            await message.reply_text(f"❌ एरर आ गया भाई: {str(e)}")
            
        finally:
            # Cleanup: Server par space bachane ke liye files turant delete karna
            if os.path.exists(input_path):
                os.remove(input_path)
            if os.path.exists(output_path):
                os.remove(output_path)
    else:
        await message.reply_text("⚠️ भाई, सिर्फ .apk फाइल ही भेज तभी बोट काम करेगा!")

def main():
    if not BOT_TOKEN:
        print("[CRITICAL] BOT_TOKEN environment variable nahi mila!")
        return
        
    # Keep-Alive Web Server chalu karo taaki 15 min mein render sleep na ho
    keep_alive()
    
    # Telegram Bot Build karo
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # Handlers add karna (/start aur APK document ke liye)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_apk))
    
    print("🤖 Pranav Master Bot 24/7 mode mein chalu ho gaya hai!")
    application.run_polling()

if __name__ == '__main__':
    main()
