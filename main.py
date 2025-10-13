import os
import logging
import httpx
import telebot
from telebot import types
from dotenv import load_dotenv

# ──────────────────────────────────────────────
# 🔧 Настройки и переменные окружения
# ──────────────────────────────────────────────
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
YCLIENTS_PARTNER_TOKEN = os.getenv("YCLIENTS_PARTNER_TOKEN")
YCLIENTS_COMPANY_ID = os.getenv("YCLIENTS_COMPANY_ID")
YCLIENTS_LOGIN = os.getenv("YCLIENTS_LOGIN")
YCLIENTS_PASSWORD = os.getenv("YCLIENTS_PASSWORD")

bot = telebot.TeleBot(BOT_TOKEN)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kutikula_bot")

API_URL = "https://api.yclients.com/api/v1"

# ──────────────────────────────────────────────
# 🧩 Авторизация в YCLIENTS (через логин/пароль)
# ──────────────────────────────────────────────
async def yclients_get_user_token():
    url = f"{API_URL}/auth"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/vnd.yclients.v2+json",
        "Authorization": f"Bearer {YCLIENTS_PARTNER_TOKEN}"
    }
    payload = {
        "login": YCLIENTS_LOGIN,
        "password": YCLIENTS_PASSWORD
    }

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(url, headers=headers, json=payload)
        data = response.json()
        if data.get("success") and data.get("data", {}).get("user_token"):
            return data["data"]["user_token"]
        logger.error(f"❌ Ошибка авторизации YCLIENTS: {data}")
        return None


# ──────────────────────────────────────────────
# 📋 Получить список услуг
# ──────────────────────────────────────────────
async def yclients_get_services(user_token: str):
    url = f"{API_URL}/company/{YCLIENTS_COMPANY_ID}/services"
    headers = {
        "Accept": "application/vnd.yclients.v2+json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {YCLIENTS_PARTNER_TOKEN}, User {user_token}"
    }

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(url, headers=headers)
        data = response.json()
        if data.get("success"):
            return data["data"]
        logger.error(f"❌ Ошибка получения услуг: {data}")
        return None


# ──────────────────────────────────────────────
# 📅 Получить записи клиента
# ──────────────────────────────────────────────
async def yclients_get_records(user_token: str):
    url = f"{API_URL}/records/{YCLIENTS_COMPANY_ID}"
    headers = {
        "Accept": "application/vnd.yclients.v2+json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {YCLIENTS_PARTNER_TOKEN}, User {user_token}"
    }

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(url, headers=headers)
        data = response.json()
        if data.get("success"):
            return data["data"]
        logger.error(f"❌ Ошибка получения записей: {data}")
        return None


# ──────────────────────────────────────────────
# ❌ Отмена записи
# ──────────────────────────────────────────────
async def yclients_cancel_record(user_token: str, record_id: int):
    url = f"{API_URL}/record/{YCLIENTS_COMPANY_ID}/{record_id}/delete"
    headers = {
        "Accept": "application/vnd.yclients.v2+json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {YCLIENTS_PARTNER_TOKEN}, User {user_token}"
    }

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(url, headers=headers)
        data = response.json()
        if data.get("success"):
            return True
        logger.error(f"❌ Ошибка отмены записи: {data}")
        return False

# ──────────────────────────────────────────────
# 🤖 Telegram-бот
# ──────────────────────────────────────────────
@bot.message_handler(commands=["start"])
def start(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("📋 Услуги", "🗓 Мои записи", "❌ Отменить запись")
    bot.send_message(message.chat.id, "Привет! Я бот для записи и управления визитами 💅", reply_markup=markup)


@bot.message_handler(func=lambda m: m.text == "📋 Услуги")
def handle_services(message):
    import asyncio
    asyncio.run(send_services_list(message))


async def send_services_list(message):
    token = await yclients_get_user_token()
    if not token:
        bot.send_message(message.chat.id, "Не удалось авторизоваться 😔")
        return

    services = await yclients_get_services(token)
    if not services:
        bot.send_message(message.chat.id, "Не удалось загрузить список услуг.")
        return

    text = "📋 *Список услуг:*\n\n"
    for s in services:
        text += f"• {s['title']} — {s.get('price_min', '—')}₽\n"
    bot.send_message(message.chat.id, text, parse_mode="Markdown")


@bot.message_handler(func=lambda m: m.text == "🗓 Мои записи")
def handle_records(message):
    import asyncio
    asyncio.run(send_user_records(message))


async def send_user_records(message):
    token = await yclients_get_user_token()
    if not token:
        bot.send_message(message.chat.id, "Не удалось авторизоваться 😔")
        return

    records = await yclients_get_records(token)
    if not records:
        bot.send_message(message.chat.id, "У вас нет активных записей.")
        return

    text = "🗓 *Ваши записи:*\n\n"
    for r in records:
        text += f"• {r['services'][0]['title']} — {r['date']} ({r['staff']['name']})\n"
    bot.send_message(message.chat.id, text, parse_mode="Markdown")


@bot.message_handler(func=lambda m: m.text == "❌ Отменить запись")
def handle_cancel(message):
    bot.send_message(message.chat.id, "Введите ID записи, которую хотите отменить.")


@bot.message_handler(func=lambda m: m.text.isdigit())
def cancel_record_by_id(message):
    import asyncio
    record_id = int(message.text)
    asyncio.run(cancel_record_action(message, record_id))


async def cancel_record_action(message, record_id):
    token = await yclients_get_user_token()
    if not token:
        bot.send_message(message.chat.id, "Не удалось авторизоваться 😔")
        return

    success = await yclients_cancel_record(token, record_id)
    if success:
        bot.send_message(message.chat.id, f"✅ Запись №{record_id} успешно отменена.")
    else:
        bot.send_message(message.chat.id, "⚠️ Не удалось отменить запись.")

# ──────────────────────────────────────────────
# 🚀 Запуск
# ──────────────────────────────────────────────
if name == "__main__":
    logger.info("Запуск Telegram-бота...")
    bot.infinity_polling()
