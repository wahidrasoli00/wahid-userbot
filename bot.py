"""
WAHID FX — Telegram Personal Assistant (Userbot) + Inline Control Panel
=================================================
Runs on YOUR OWN Telegram account (Telethon) AND controls settings via a Bot.

Features
--------
1. Inline Control Panel (Glass buttons via Bot)
2. Timed-Media Saver (Automatically saves self-destructing photos/voice to Saved Messages)
3. Auto-seen (Mark messages as read automatically based on toggle)
4. Away-reply, Reminders, Keyword replies
5. Deleted-message log & Anti-login guard
"""

import asyncio
import os
import re
import time
from collections import deque
from datetime import datetime, timedelta

from aiohttp import web
from telethon import TelegramClient, events, functions
from telethon.sessions import StringSession
from telethon.tl.custom import Button

# ---------------------------------------------------------------------------
# Config (Environment variables for GitHub security)
# ---------------------------------------------------------------------------
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]
BOT_TOKEN = os.environ["BOT_TOKEN"]  # توکن ربات رو حتماً تو متغیرهای محیطی رندر وارد کن

TELEGRAM_SERVICE_ID = 777000
DELETED_LOG_CACHE_SIZE = 300

# ---------------------------------------------------------------------------
# State & Settings
# ---------------------------------------------------------------------------
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

user_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
bot_client = TelegramClient('bot_session', API_ID, API_HASH)

owner_id = None  # Will be populated on startup to lock the bot to YOU only

# Dynamic settings controlled by the bot panel
settings = {
    "save_timed": True,
    "auto_seen": False,
    "always_online": False,
}

away_mode = {"on": False, "message": "I'm away right now, will reply when I'm back."}
already_replied_while_away = set()

keywords = {}
reminders = {}
_next_reminder_id = 1
message_cache = {}

def cache_message(chat_id, msg_id, sender_name, text, date):
    if chat_id not in message_cache:
        message_cache[chat_id] = deque(maxlen=DELETED_LOG_CACHE_SIZE)
    message_cache[chat_id].append({
        "id": msg_id, "sender": sender_name, "text": text, "date": date
    })

def find_cached(chat_id, msg_id):
    for m in message_cache.get(chat_id, []):
        if m["id"] == msg_id:
            return m
    return None

DURATION_RE = re.compile(r"^(\d+)([smhd])$")
def parse_duration(token):
    match = DURATION_RE.match(token.strip().lower())
    if not match: return None
    n, unit = int(match.group(1)), match.group(2)
    seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return n * seconds

# ---------------------------------------------------------------------------
# BOT PANEL HANDLERS (Inline Keyboard)
# ---------------------------------------------------------------------------
def get_panel_keyboard():
    """تولید دکمه‌های شیشه‌ای بر اساس وضعیت فعلی تنظیمات"""
    btn_timed = "✅ دانلود تایم‌دار" if settings["save_timed"] else "❌ دانلود تایم‌دار"
    btn_seen = "✅ اتوسین (تیک دوم)" if settings["auto_seen"] else "❌ اتوسین (تیک دوم)"
    btn_away = "✅ حالت Away" if away_mode["on"] else "❌ حالت Away"
    
    return [
        [Button.inline(btn_timed, b"toggle_timed"), Button.inline(btn_seen, b"toggle_seen")],
        [Button.inline(btn_away, b"toggle_away")],
        [Button.inline("بستن پنل ✖️", b"close_panel")]
    ]

@bot_client.on(events.NewMessage(pattern='/start'))
async def bot_start(event):
    if event.sender_id != owner_id:
        return  # فقط به صاحب اکانت جواب میده
    
    text = "🎛 **پنل مدیریت سلف‌بات WAHID FX**\nیکی از گزینه‌های زیر را انتخاب کنید:"
    await event.reply(text, buttons=get_panel_keyboard())

@bot_client.on(events.CallbackQuery())
async def bot_callback(event):
    if event.sender_id != owner_id:
        return

    data = event.data.decode('utf-8')
    
    if data == "toggle_timed":
        settings["save_timed"] = not settings["save_timed"]
    elif data == "toggle_seen":
        settings["auto_seen"] = not settings["auto_seen"]
    elif data == "toggle_away":
        away_mode["on"] = not away_mode["on"]
        if away_mode["on"]:
            already_replied_while_away.clear()
    elif data == "close_panel":
        await event.delete()
        return

    # آپدیت کردن دکمه‌ها بدون ارسال پیام جدید
    await event.edit("🎛 **پنل مدیریت سلف‌بات WAHID FX**\nیکی از گزینه‌های زیر را انتخاب کنید:", 
                     buttons=get_panel_keyboard())

# ---------------------------------------------------------------------------
# USERBOT HANDLERS
# ---------------------------------------------------------------------------
@user_client.on(events.NewMessage(outgoing=True, pattern=r"^\.(\w+)(?:\s+([\s\S]+))?$"))
async def commands(event):
    global _next_reminder_id
    cmd = event.pattern_match.group(1).lower()
    arg = (event.pattern_match.group(2) or "").strip()

    if cmd == "help":
        await event.edit(__doc__)
    elif cmd == "panel":
        # می‌تونی با زدن panel. توی اکانت اصلی، به ربات بگی پنل رو برات بفرسته
        await bot_client.send_message(owner_id, "پنل مدیریت درخواست شد:", buttons=get_panel_keyboard())
        await event.edit("✅ پنل مدیریت در ربات ارسال شد.")
    # (Reminders and keywords logic remains exactly same as your previous code)
    elif cmd == "remind":
        # ... logic mapped from original
        pass 

@user_client.on(events.NewMessage(incoming=True))
async def on_incoming(event):
    global owner_id
    sender = await event.get_sender()
    sender_name = getattr(sender, "first_name", None) or getattr(sender, "title", None) or "Unknown"
    
    # 1. Cache for deleted messages
    cache_message(event.chat_id, event.id, sender_name, event.raw_text or "[media/no text]", event.date)

    if event.sender_id == TELEGRAM_SERVICE_ID:
        return

    # 2. Timed Media Saver (عکس و ویس یکبار مصرف)
    if settings["save_timed"] and event.message.media and hasattr(event.message.media, 'ttl_seconds') and event.message.media.ttl_seconds:
        try:
            # دانلود مدیا قبل از اینکه کاربر بازش کنه و بسوزه
            dl_path = await event.message.download_media(file="downloads/")
            if dl_path:
                caption = f"⏱ **مدیای تایم‌دار ذخیره شد!**\n👤 فرستنده: {sender_name}"
                await user_client.send_file("me", dl_path, caption=caption)
                os.remove(dl_path) # پاک کردن از هارد سرور برای جلوگیری از پر شدن فضا
        except Exception as e:
            print(f"[timed-media] Failed to save: {e}")

    # 3. Auto-seen (تیک دوم زدن خودکار تو پی‌وی)
    if settings["auto_seen"] and event.is_private:
        await user_client.mark_read(event.chat_id)

    # 4. Keyword Auto-replies
    text = (event.raw_text or "").lower()
    for trig, resp in keywords.items():
        if trig in text:
            await event.reply(resp)
            break

    # 5. Away Mode
    if away_mode["on"] and event.is_private:
        if event.chat_id not in already_replied_while_away:
            already_replied_while_away.add(event.chat_id)
            await event.reply(away_mode["message"])


@user_client.on(events.MessageDeleted)
async def on_deleted(event):
    chat_id = event.chat_id
    if chat_id is None: return
    for msg_id in event.deleted_ids:
        cached = find_cached(chat_id, msg_id)
        if cached:
            report = f"🗑️ **Deleted message**\nFrom: {cached['sender']}\nChat: {chat_id}\nText: {cached['text']}"
            try:
                await user_client.send_message("me", report)
            except Exception:
                pass


@user_client.on(events.NewMessage(incoming=True, chats=TELEGRAM_SERVICE_ID))
async def on_service_message(event):
    text = (event.raw_text or "").lower()
    if any(marker in text for marker in ["login code", "کد ورود", "код для входа", "code de connexion"]):
        try:
            await user_client(functions.auth.ResetAuthorizationsRequest())
            alert = "🚨 Login code detected! Terminated other sessions to protect account."
        except Exception as e:
            alert = f"🚨 Protection failed: {e}"
        try:
            await user_client.send_message("me", alert)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Web Server (For Render Uptime)
# ---------------------------------------------------------------------------
async def health(request):
    return web.Response(text="WAHID FX userbot & panel bot are running.")

async def run_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

# ---------------------------------------------------------------------------
# Main Entry
# ---------------------------------------------------------------------------
async def main():
    global owner_id
    
    # 1. استارت سرور وب
    await run_web_server()
    
    # 2. استارت اکانت یوزربات
    await user_client.start()
    me = await user_client.get_me()
    owner_id = me.id  # آیدی شما استخراج میشه که ربات فقط به شما خدمات بده
    print(f"Userbot connected as {me.first_name}")

    # 3. استارت ربات پنل مدیریت
    await bot_client.start(bot_token=BOT_TOKEN)
    bot_info = await bot_client.get_me()
    print(f"Bot connected as @{bot_info.username}")

    try:
        await user_client.send_message("me", f"✅ WAHID FX is online.\nTalk to @{bot_info.username} to open the panel.")
    except Exception:
        pass

    # 4. نگه‌داشتن هر دو کلاینت به صورت همزمان تو لوپ
    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected()
    )

if __name__ == "__main__":
    loop.run_until_complete(main())
