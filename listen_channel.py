import json
import os
import re
import sys

import requests
from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow
from telethon import TelegramClient, events


def get_app_folder():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


APP_FOLDER = get_app_folder()
load_dotenv(os.path.join(APP_FOLDER, ".env"))

FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL", "").rstrip("/")
CHANNEL_NAME = os.getenv("CHANNEL_NAME")

TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")

GOOGLE_CLIENT_FILE = os.path.join(APP_FOLDER, "google-oauth-client.json")
SESSION_FILE = os.path.join(APP_FOLDER, "firebase_session.json")
TXT_BACKUP_FILE = os.path.join(APP_FOLDER, "valid_signals.txt")

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile"
]

firebase_id_token = None
firebase_refresh_token = None
firebase_uid = None

client = None


def save_firebase_session(refresh_token):
    with open(SESSION_FILE, "w", encoding="utf-8") as file:
        json.dump({"refresh_token": refresh_token}, file)


def load_firebase_session():
    if not os.path.exists(SESSION_FILE):
        return None

    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            return data.get("refresh_token")
    except Exception:
        return None


def refresh_firebase_login(refresh_token):
    global firebase_id_token, firebase_refresh_token

    url = f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}"

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token
    }

    response = requests.post(url, data=payload, timeout=20)
    data = response.json()

    if not response.ok:
        print("Saved Firebase login expired or invalid.")
        return False

    firebase_id_token = data["id_token"]
    firebase_refresh_token = data["refresh_token"]

    save_firebase_session(firebase_refresh_token)

    print("Firebase login restored/refreshed.")
    return True


def google_login():
    flow = InstalledAppFlow.from_client_secrets_file(
        GOOGLE_CLIENT_FILE,
        scopes=SCOPES
    )

    credentials = flow.run_local_server(
        port=0,
        prompt="select_account"
    )

    return credentials.id_token


def firebase_login_with_google(google_id_token):
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithIdp?key={FIREBASE_API_KEY}"

    payload = {
        "postBody": f"id_token={google_id_token}&providerId=google.com",
        "requestUri": "http://localhost",
        "returnIdpCredential": True,
        "returnSecureToken": True
    }

    response = requests.post(url, json=payload, timeout=20)
    data = response.json()

    if not response.ok:
        raise Exception(f"Firebase login failed: {data}")

    return data


def initialize_firebase_login():
    global firebase_id_token, firebase_refresh_token, firebase_uid

    saved_refresh_token = load_firebase_session()

    if saved_refresh_token:
        if refresh_firebase_login(saved_refresh_token):
            firebase_uid = "restored_session"
            return True

    print("Opening Google login...")
    google_id_token = google_login()

    print("Logging in to Firebase...")
    firebase_user = firebase_login_with_google(google_id_token)

    firebase_id_token = firebase_user["idToken"]
    firebase_refresh_token = firebase_user["refreshToken"]
    firebase_uid = firebase_user["localId"]

    save_firebase_session(firebase_refresh_token)

    print("Firebase login successful")
    print("UID:", firebase_uid)
    print("Email:", firebase_user.get("email"))

    return True


def parse_signal_message(text):
    if not text:
        return None

    pair_match = re.search(r"PAIR:\s*#?([A-Z0-9]+)", text, re.IGNORECASE)
    type_match = re.search(r"TYPE:\s*(BUY|SELL)", text, re.IGNORECASE)
    entry_match = re.search(r"Entry:\s*([\d.]+)\s+([\d.]+)", text, re.IGNORECASE)
    tp1_match = re.search(r"TP1:\s*([\d.]+)", text, re.IGNORECASE)
    tp2_match = re.search(r"TP2:\s*([\d.]+)", text, re.IGNORECASE)
    sl_match = re.search(r"SL:\s*([\d.]+)", text, re.IGNORECASE)

    if not all([pair_match, type_match, entry_match, tp1_match, tp2_match, sl_match]):
        return None

    return {
        "pair": pair_match.group(1).upper(),
        "type": type_match.group(1).upper(),
        "entry_1": float(entry_match.group(1)),
        "entry_2": float(entry_match.group(2)),
        "tp1": float(tp1_match.group(1)),
        "tp2": float(tp2_match.group(1)),
        "sl": float(sl_match.group(1)),
    }


def save_signal_to_firebase(message_id, date, signal_data):
    global firebase_id_token

    data = {
        "message_id": message_id,
        "pair": signal_data["pair"],
        "type": signal_data["type"],
        "entry_1": signal_data["entry_1"],
        "entry_2": signal_data["entry_2"],
        "tp1": signal_data["tp1"],
        "tp2": signal_data["tp2"],
        "sl": signal_data["sl"],
        "telegram_date": date.isoformat(),
        "source_uid": firebase_uid,
        "processed": False
    }

    url = f"{DATABASE_URL}/signals/{CHANNEL_NAME}/{message_id}.json?auth={firebase_id_token}"

    response = requests.put(url, json=data, timeout=20)

    if response.status_code == 200:
        print("Saved to Firebase:", message_id)
        return True

    if response.status_code in [401, 403]:
        print("Firebase write failed. Trying to refresh token...")

        if firebase_refresh_token and refresh_firebase_login(firebase_refresh_token):
            url = f"{DATABASE_URL}/signals/{CHANNEL_NAME}/{message_id}.json?auth={firebase_id_token}"
            response = requests.put(url, json=data, timeout=20)

            if response.status_code == 200:
                print("Saved to Firebase after token refresh:", message_id)
                return True

        print("No Firebase write permission or token refresh failed. Signal ignored:", message_id)
        print(response.text)
        return False

    print("Firebase save failed:", response.status_code)
    print(response.text)
    return False


def save_signal_to_txt(message_id, date, signal_data):
    with open(TXT_BACKUP_FILE, "a", encoding="utf-8") as file:
        file.write(f"Message ID: {message_id}\n")
        file.write(f"Date: {date}\n")
        file.write(f"Pair: {signal_data['pair']}\n")
        file.write(f"Type: {signal_data['type']}\n")
        file.write(f"Entry 1: {signal_data['entry_1']}\n")
        file.write(f"Entry 2: {signal_data['entry_2']}\n")
        file.write(f"TP1: {signal_data['tp1']}\n")
        file.write(f"TP2: {signal_data['tp2']}\n")
        file.write(f"SL: {signal_data['sl']}\n")
        file.write("-" * 40 + "\n")


@events.register(events.NewMessage(chats=CHANNEL_NAME))
async def handle_new_message(event):
    text = event.message.message

    signal_data = parse_signal_message(text)

    if signal_data is None:
        print("Ignored message. Not a signal format.")
        return

    message_id = event.message.id
    date = event.message.date

    print("Valid signal found:")
    print(signal_data)

    save_signal_to_txt(message_id, date, signal_data)
    save_signal_to_firebase(message_id, date, signal_data)


def check_env():
    required_values = {
        "FIREBASE_API_KEY": FIREBASE_API_KEY,
        "DATABASE_URL": DATABASE_URL,
        "CHANNEL_NAME": CHANNEL_NAME,
        "TELEGRAM_API_ID": TELEGRAM_API_ID,
        "TELEGRAM_API_HASH": TELEGRAM_API_HASH
    }

    for key, value in required_values.items():
        if not value:
            print(f"{key} is missing in .env")
            return False

    if not os.path.exists(GOOGLE_CLIENT_FILE):
        print("google-oauth-client.json not found")
        return False

    return True


def main():
    global client

    if not check_env():
        return

    if not initialize_firebase_login():
        return

    client = TelegramClient(
        os.path.join(APP_FOLDER, "client_telegram_session"),
        int(TELEGRAM_API_ID),
        TELEGRAM_API_HASH
    )

    client.add_event_handler(handle_new_message)

    print("Starting Telegram listener...")
    client.start()

    print(f"Listening to Telegram channel: {CHANNEL_NAME}")
    client.run_until_disconnected()


if __name__ == "__main__":
    main()