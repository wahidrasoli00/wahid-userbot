"""
WAHID FX — Telegram Personal Assistant (Userbot)
=================================================
Runs on YOUR OWN Telegram account (not a separate bot account) using Telethon.
Only responds to commands YOU send yourself — never acts on other people's messages.

Features
--------
1. Away-reply       : auto-reply once per chat while you're marked "away"
2. Reminders         : ".remind 10m Buy milk" -> pings you back in Saved Messages
3. Keyword replies    : configurable trigger -> response map
4. Deleted-message log: caches recent messages, logs anything that gets deleted
5. Anti-login guard   : when Telegram's official login-code notification arrives,
                        immediately terminates every OTHER active session to protect
                        your account, then alerts you in Saved Messages.

Commands (send these to yourself / Saved Messages, or in any chat — they only
fire when YOU are the sender):
    .away <message>      turn away-mode ON with a custom auto-reply
    .back                turn away-mode OFF
    .remind <time> <text>  e.g. ".remind 25m stretch break" or ".remind 2h call mom"
    .reminders            list pending reminders
    .cancelreminder <id>  cancel a reminder by its id
    .addkeyword <trigger> | <response>
    .delkeyword <trigger>
    .keywords              list configured keyword replies
    .help                  show this command list
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
from telethon.tl.types import UpdateShort

# ---------------------------------------------------------------------------
# Config (all from environment variables — never hardcode secrets in the file)
# ---------------------------------------------------------------------------
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]

# Official Telegram service notifications account (login codes, etc.)
TELEGRAM_SERVICE_ID = 777000

# How many recent messages per chat to keep in memory for the deleted-message log
DELETED_LOG_CACHE_SIZE = 300

# Persian "glass panel" style command menu, shown by .panel / .پنل
PANEL_TEXT = (
    "╭───────────────────╮\n"
    "   **✨ پنل WAHID FX ✨**\n"
    "╰───────────────────╯\n\n"
    "**🌙 حالت آفلاین**\n"
    "`.away <پیام>`  →  روشن کردن پاسخ خودکار\n"
    "`.back`  →  خاموش کردن\n\n"
    "**⏰ یادآوری**\n"
    "`.remind <10m|2h|1d> <متن>`\n"
    "`.reminders`  →  لیست یادآوری‌های باز\n"
    "`.cancelreminder <شماره>`\n\n"
    "**🔑 کلمه کلیدی**\n"
    "`.addkeyword <کلمه> | <پاسخ>`\n"
    "`.delkeyword <کلمه>`\n"
    "`.keywords`  →  لیست کلمات فعال\n\n"
    "**🛡️ محافظت (خودکار، بدون دستور)**\n"
    "• پیام‌های حذف‌شده → لاگ میشه اینجا\n"
    "• آنتی‌لاگین → سشن‌های مشکوک قطع میشه\n\n"
    "**📋 راهنما**\n"
    "`.panel` یا `.help`  →  همین صفحه"
)

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
# Python 3.12+ (Render's default runtime) no longer auto-creates an event
# loop for the main thread. Telethon's client needs one to exist BEFORE it's
# constructed, so we create and set it explicitly here.
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
client.parse_mode = "markdown"  # lets us send **bold** text in the panel/help output

away_mode = {"on": False, "message": ""}
already_replied_while_away = set()  # chat_ids already auto-replied to this away session

keywords = {}  # trigger(lower) -> response

reminders = {}  # id(int) -> {"chat_id":.., "text":.., "due": datetime}
_next_reminder_id = 1

# message cache for deleted-message detection: chat_id -> deque of (msg_id, sender, text, date)
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


# ---------------------------------------------------------------------------
# Helper: parse "10m" / "2h" / "1d" style durations
# ---------------------------------------------------------------------------
DURATION_RE = re.compile(r"^(\d+)([smhd])$")

def parse_duration(token):
    match = DURATION_RE.match(token.strip().lower())
    if not match:
        return None
    n, unit = int(match.group(1)), match.group(2)
    seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return n * seconds


# ---------------------------------------------------------------------------
# Command handler (only fires on YOUR OWN outgoing messages)
# ---------------------------------------------------------------------------
@client.on(events.NewMessage(outgoing=True, pattern=r"^\.(\w+)(?:\s+([\s\S]+))?$"))
async def commands(event):
    global _next_reminder_id

    cmd = event.pattern_match.group(1).lower()
    arg = (event.pattern_match.group(2) or "").strip()

    if cmd == "help":
        await event.edit(__doc__)
        return

    if cmd in ("panel", "پنل"):
        await event.edit(PANEL_TEXT)
        return

    if cmd == "away":
        away_mode["on"] = True
        away_mode["message"] = arg or "I'm away right now, will reply when I'm back."
        already_replied_while_away.clear()
        await event.edit(f"🌙 Away mode ON.\nAuto-reply: {away_mode['message']}")
        return

    if cmd == "back":
        away_mode["on"] = False
        already_replied_while_away.clear()
        await event.edit("☀️ Away mode OFF. Welcome back.")
        return

    if cmd == "remind":
        parts = arg.split(maxsplit=1)
        if len(parts) < 2:
            await event.edit("Usage: .remind <10m|2h|1d> <text>")
            return
        seconds = parse_duration(parts[0])
        if seconds is None:
            await event.edit("Couldn't parse the time. Use formats like 10m, 2h, 1d.")
            return
        text = parts[1]
        rid = _next_reminder_id
        _next_reminder_id += 1
        due = datetime.now() + timedelta(seconds=seconds)
        reminders[rid] = {"chat_id": event.chat_id, "text": text, "due": due}
        asyncio.create_task(schedule_reminder(rid, seconds))
        await event.edit(f"⏰ Reminder #{rid} set for {due.strftime('%H:%M:%S')} — {text}")
        return

    if cmd == "reminders":
        if not reminders:
            await event.edit("No pending reminders.")
            return
        lines = [f"#{rid}: {r['text']} @ {r['due'].strftime('%H:%M:%S')}" for rid, r in reminders.items()]
        await event.edit("⏰ Pending reminders:\n" + "\n".join(lines))
        return

    if cmd == "cancelreminder":
        try:
            rid = int(arg)
        except ValueError:
            await event.edit("Usage: .cancelreminder <id>")
            return
        if rid in reminders:
            del reminders[rid]
            await event.edit(f"Reminder #{rid} cancelled.")
        else:
            await event.edit(f"No reminder with id #{rid}.")
        return

    if cmd == "addkeyword":
        if "|" not in arg:
            await event.edit("Usage: .addkeyword <trigger> | <response>")
            return
        trig, resp = [p.strip() for p in arg.split("|", 1)]
        keywords[trig.lower()] = resp
        await event.edit(f"✅ Keyword added: \"{trig}\" → \"{resp}\"")
        return

    if cmd == "delkeyword":
        if arg.lower() in keywords:
            del keywords[arg.lower()]
            await event.edit(f"🗑️ Keyword \"{arg}\" removed.")
        else:
            await event.edit(f"No keyword \"{arg}\" found.")
        return

    if cmd == "keywords":
        if not keywords:
            await event.edit("No keywords configured.")
            return
        lines = [f"\"{k}\" → \"{v}\"" for k, v in keywords.items()]
        await event.edit("🔑 Keywords:\n" + "\n".join(lines))
        return


async def schedule_reminder(rid, seconds):
    await asyncio.sleep(seconds)
    r = reminders.pop(rid, None)
    if r is None:
        return  # was cancelled
    try:
        await client.send_message(r["chat_id"], f"⏰ Reminder: {r['text']}")
    except Exception as e:
        print(f"[reminder] failed to send: {e}")


# ---------------------------------------------------------------------------
# Incoming message watcher — away-reply, keyword replies, message caching
# ---------------------------------------------------------------------------
@client.on(events.NewMessage(incoming=True))
async def on_incoming(event):
    # cache for deleted-message detection
    sender = await event.get_sender()
    sender_name = getattr(sender, "first_name", None) or getattr(sender, "title", None) or "Unknown"
    cache_message(event.chat_id, event.id, sender_name, event.raw_text or "[media/no text]", event.date)

    # ignore private-chat automation for the official Telegram service account here;
    # that's handled separately by the anti-login guard below
    if event.sender_id == TELEGRAM_SERVICE_ID:
        return

    text = (event.raw_text or "").lower()

    # keyword auto-replies
    for trig, resp in keywords.items():
        if trig in text:
            await event.reply(resp)
            break

    # away auto-reply (once per chat per away session), only in private chats
    if away_mode["on"] and event.is_private:
        if event.chat_id not in already_replied_while_away:
            already_replied_while_away.add(event.chat_id)
            await event.reply(away_mode["message"])


# ---------------------------------------------------------------------------
# Deleted-message logger
# ---------------------------------------------------------------------------
@client.on(events.MessageDeleted)
async def on_deleted(event):
    chat_id = event.chat_id
    if chat_id is None:
        return
    for msg_id in event.deleted_ids:
        cached = find_cached(chat_id, msg_id)
        if cached is None:
            continue
        report = (
            f"🗑️ Deleted message\n"
            f"From: {cached['sender']}\n"
            f"Chat: {chat_id}\n"
            f"Time: {cached['date'].strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Text: {cached['text']}"
        )
        try:
            await client.send_message("me", report)  # logs to Saved Messages
        except Exception as e:
            print(f"[deleted-log] failed to send: {e}")


# ---------------------------------------------------------------------------
# Anti-login guard — protects the account if someone tries to log in elsewhere
# ---------------------------------------------------------------------------
LOGIN_CODE_MARKERS = ["login code", "کد ورود", "код для входа", "code de connexion"]

@client.on(events.NewMessage(incoming=True, chats=TELEGRAM_SERVICE_ID))
async def on_service_message(event):
    text = (event.raw_text or "").lower()
    if not any(marker in text for marker in LOGIN_CODE_MARKERS):
        return

    try:
        await client(functions.auth.ResetAuthorizationsRequest())
        alert = (
            "🚨 Login code notification detected!\n"
            "All other active sessions were just terminated to protect this account.\n"
            "If that was YOU logging in on a new device, just log in again — "
            "this only affects sessions other than this bot's own connection."
        )
    except Exception as e:
        if "too new" in str(e).lower():
            alert = (
                "🚨 Login code notification detected!\n"
                "Telegram won't let a session younger than 24 hours terminate other "
                "sessions (an anti-hijack rule on Telegram's side, not a bug here). "
                "This protection will start working automatically once this bot's "
                "session turns 24 hours old — no action needed from you. "
                "If this wasn't you logging in, log in and check your active "
                "sessions manually for now (Settings → Devices)."
            )
        else:
            alert = f"🚨 Login code notification detected, but auto-protection failed: {e}"

    try:
        await client.send_message("me", alert)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tiny web server — Render's free tier only exists for "Web Service" type,
# which requires binding to a port and answering HTTP requests. This endpoint
# does nothing but say "OK" so Render is happy, and so an external uptime
# pinger (see README) can hit it every few minutes to stop the free instance
# from spinning down after 15 minutes of inactivity.
# ---------------------------------------------------------------------------
async def health(request):
    return web.Response(text="WAHID FX userbot is running.")

async def run_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[web] health server listening on port {port}")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
async def main():
    await run_web_server()
    await client.start()
    me = await client.get_me()
    print(f"WAHID FX userbot connected as {me.first_name} (@{me.username}). Listening…")
    try:
        await client.send_message("me", "✅ WAHID FX userbot is online.")
    except Exception:
        pass
    await client.run_until_disconnected()


if __name__ == "__main__":
    loop.run_until_complete(main())
