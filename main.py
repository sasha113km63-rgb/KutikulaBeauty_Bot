import os
import telebot
import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

# ------------------- Загрузка переменных окружения -------------------
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
YCLIENTS_API_BASE = os.getenv("YCLIENTS_API_BASE", "https://api.yclients.com/api/v1/")
YCLIENTS_COMPANY_ID = os.getenv("YCLIENTS_COMPANY_ID")
YCLIENTS_PARTNER_TOKEN = os.getenv("YCLIENTS_PARTNER_TOKEN")
YCLIENTS_LOGIN = os.getenv("YCLIENTS_LOGIN")
YCLIENTS_PASSWORD = os.getenv("YCLIENTS_PASSWORD")

if not TELEGRAM_TOKEN:
    raise ValueError("❌ TELEGRAM_TOKEN не найден. Проверь настройки окружения Render.")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = FastAPI()


# ------------------- Проверка подключения к YCLIENTS -------------------
def yclients_auth():
    """Авторизация по логину и паролю (без SMS)."""
    url = f"{YCLIENTS_API_BASE}auth"
    headers = {
        "Accept": "application/vnd.yclients.v2+json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {YCLIENTS_PARTNER_TOKEN}"
    }
    data = {
        "login": YCLIENTS_LOGIN,
        "password": YCLIENTS_PASSWORD
    }

    response = requests.post(url, headers=headers, json=data)
    if response.status_code == 200 and response.json().get("success"):
        return response.json()["data"]["user_token"]
    else:
        print("❌ Ошибка авторизации YCLIENTS:", response.text)
        return None


# ------------------- Обработчики Telegram -------------------
@bot.message_handler(commands=["start"])
def start(message):
    bot.send_message(
        message.chat.id,
        "Привет 👋\nЯ — бот для записи в Kutikula Beauty.\n\n"
        "Пока я умею:\n"
        "🗓 Проверять подключение к YCLIENTS\n"
        "❌ Отменять запись (скоро)\n"
        "👀 Смотреть свои брони (скоро)\n\n"
        "Для проверки подключения напиши: /check"
    )


@bot.message_handler(commands=["check"])
def check_yclients(message):
    token = yclients_auth()
    if token:
        bot.send_message(message.chat.id, "✅ Подключение к YCLIENTS успешно!")
    else:
        bot.send_message(message.chat.id, "❌ Ошибка авторизации в YCLIENTS.")


@bot.message_handler(func=lambda message: True)
def echo_message(message):
    bot.reply_to(message, "Напишите /start, чтобы начать.")


# ------------------- FastAPI webhook -------------------
@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    """Получаем обновления от Telegram"""
    data = await request.json()
    update = telebot.types.Update.de_json(data)
    bot.process_new_updates([update])
    return JSONResponse(status_code=200, content={"ok": True})


# ------------------- Health check -------------------
@app.get("/")
def home():
    return {"status": "ok", "message": "Kutikula Beauty Bot работает 🩷"}


# ------------------- Запуск -------------------
if name == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
