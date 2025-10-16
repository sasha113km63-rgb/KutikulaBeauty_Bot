import os
from dotenv import load_dotenv

# Загружаем переменные из .env
load_dotenv()

# --- Telegram ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# --- YCLIENTS ---
YCLIENTS_PARTNER_TOKEN = os.getenv("YCLIENTS_PARTNER_TOKEN")
YCLIENTS_LOGIN = os.getenv("YCLIENTS_LOGIN")
YCLIENTS_PASSWORD = os.getenv("YCLIENTS_PASSWORD")

# --- Company ---
YCLIENTS_COMPANY_ID = int(os.getenv("YCLIENTS_COMPANY_ID", 530777))

# --- Прочие настройки ---
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
