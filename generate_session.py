"""
Run this ONCE on your own computer (not on Render) to log in and generate
a SESSION_STRING. You'll need your API_ID and API_HASH from https://my.telegram.org
It will ask for your phone number, then the login code Telegram sends you,
then (if enabled) your 2FA password.

Usage:
    pip install telethon
    python generate_session.py

Copy the printed string and save it somewhere safe — treat it exactly like a
password. Anyone with this string has full access to your Telegram account.
"""

from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID = int(input("API_ID: ").strip())
API_HASH = input("API_HASH: ").strip()

with TelegramClient(StringSession(), API_ID, API_HASH) as client:
    session_string = client.session.save()
    print("\n=========================================================")
    print("Your SESSION_STRING (copy everything between the lines):")
    print("=========================================================")
    print(session_string)
    print("=========================================================")
    print("Keep this secret. Set it as the SESSION_STRING env var on Render.")
