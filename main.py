import os
import telebot
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request

# Загружаем переменные окружения
load_dotenv()

# Telegram токен
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# YCLIENTS параметры
YCLIENTS_API_BASE = os.getenv("YCLIENTS_API_BASE", "https://api.yclients.com/api/v1")
YCLIENTS_COMPANY_ID = os.getenv("YCLIENTS_COMPANY_ID")
YCLIENTS_PARTNER_TOKEN = os.getenv("YCLIENTS_PARTNER_TOKEN")
YCLIENTS_USER_LOGIN = os.getenv("YCLIENTS_USER_LOGIN")
YCLIENTS_USER_PASSWORD = os.getenv("YCLIENTS_USER_PASSWORD")

app = FastAPI()

# ---------------------- YCLIENTS AUTH ----------------------
async def get_user_token():
    """Получаем токен пользователя (авторизация через логин/пароль, без смс)"""
    url = f"{YCLIENTS_API}/auth"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    payload = {
        "login": YCLIENTS_USER_LOGIN,
        "password": YCLIENTS_USER_PASSWORD,
        "partner_token": YCLIENTS_PARTNER_TOKEN
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers)
        data = response.json()
        if "data" in data and "user_token" in data["data"]:
            return data["data"]["user_token"]
        else:
            print("Ошибка при авторизации:", data)
            return None


# ---------------------- TELEGRAM HANDLERS ----------------------
@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(message, "👋 Привет! Я бот для записи и управления записями в YCLIENTS.\n"
                          "Доступные команды:\n"
                          "/services — показать услуги\n"
                          "/bookings — мои записи")


@bot.message_handler(commands=["services"])
async def services(message):
    """Получение списка услуг"""
    user_token = await get_user_token()
    if not user_token:
        bot.reply_to(message, "Ошибка авторизации в YCLIENTS 😔")
        return

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {YCLIENTS_PARTNER_TOKEN}, User {user_token}"
    }

    url = f"{YCLIENTS_API}/company/{YCLIENTS_COMPANY_ID}/services"
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=headers)
        data = r.json()

    if not data.get("success"):
        bot.reply_to(message, f"Ошибка: {data.get('meta', {}).get('message', 'Неизвестная ошибка')}")
        return

    services = data.get("data", [])
    if not services:
        bot.reply_to(message, "Список услуг пуст.")
        return

    text = "💅 Наши услуги:\n\n"
    for s in services:
        text += f"• {s['title']} — от {s['price_min']}₽ до {s['price_max']}₽\n"
    bot.reply_to(message, text)


@bot.message_handler(commands=["bookings"])
async def bookings(message):
    """Просмотр записей пользователя"""
    user_token = await get_user_token()
    if not user_token:
        bot.reply_to(message, "Ошибка авторизации 😔")
        return

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {YCLIENTS_PARTNER_TOKEN}, User {user_token}"
    }

    url = f"{YCLIENTS_API}/records/{YCLIENTS_COMPANY_ID}"
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=headers)
        data = r.json()

    if not data.get("success"):
        bot.reply_to(message, "Ошибка получения записей.")
        return

    records = data.get("data", [])
    if not records:
        bot.reply_to(message, "У вас нет активных записей.")
        return

    text = "📅 Ваши записи:\n\n"
    for rec in records:
        text += f"— {rec['services'][0]['title']} ({rec['date']})\n"
    bot.reply_to(message, text)


# ---------------------- WEBHOOK (для Render) ----------------------
@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    json_str = await request.body()
    update = telebot.types.Update.de_json(json_str.decode("utf-8"))
    bot.process_new_updates([update])
    return {"ok": True}


@app.get("/")
def home():
    return {"status": "ok"}
