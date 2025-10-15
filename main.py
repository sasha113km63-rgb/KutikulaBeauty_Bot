import os
import json
import logging
from fastapi import FastAPI, Request
import httpx
import requests

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kutikula_bot")

# Основное приложение
app = FastAPI()

# --- Настройки из переменных окружения ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
YCLIENTS_PARTNER_TOKEN = os.getenv("YCLIENTS_PARTNER_TOKEN")
YCLIENTS_LOGIN = os.getenv("YCLIENTS_LOGIN")
YCLIENTS_PASSWORD = os.getenv("YCLIENTS_PASSWORD")

# --- Глобальный токен пользователя YCLIENTS ---
YCLIENTS_USER_TOKEN = None


# --- Авторизация в YCLIENTS ---
def yclients_auth():
    """Авторизация в YCLIENTS и получение user_token"""
    global YCLIENTS_USER_TOKEN
    try:
        url = "https://api.yclients.com/api/v1/auth"
        headers = {
            "Accept": "application/vnd.yclients.v2+json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {YCLIENTS_PARTNER_TOKEN}"
        }
        payload = {"login": YCLIENTS_LOGIN, "password": YCLIENTS_PASSWORD}

        response = requests.post(url, json=payload, headers=headers)
        data = response.json()

        if data.get("success"):
            YCLIENTS_USER_TOKEN = data["data"]["user_token"]
            logger.info("✅ Авторизация YCLIENTS успешна")
            return YCLIENTS_USER_TOKEN
        else:
            logger.error(f"❌ Ошибка авторизации YCLIENTS: {data}")
            return None
    except Exception as e:
        logger.exception("Ошибка при авторизации YCLIENTS")
        return None


# --- Получение списка услуг ---
async def try_yclients_get_services():
    """Получает список услуг компании"""
    try:
        company_id = 530777  # ID компании (замени на свой!)
        url = f"https://api.yclients.com/api/v1/company/{company_id}/services"

        headers = {
            "Accept": "application/vnd.yclients.v2+json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {YCLIENTS_PARTNER_TOKEN}, User {YCLIENTS_USER_TOKEN}"
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers)
            data = response.json()

        if data.get("success"):
            services = [srv["title"] for srv in data.get("data", [])]
            logger.info(f"✅ Получено {len(services)} услуг")
            return "\n".join(services) if services else "Список услуг пуст."
        else:
            logger.error(f"❌ Ошибка при получении услуг: {data}")
            return f"Ошибка YCLIENTS: {data.get('meta', {}).get('message', 'Неизвестная ошибка')}"
    except Exception as e:
        logger.exception("Ошибка при запросе услуг")
        return "Произошла ошибка при получении списка услуг."


# --- Telegram: обработка сообщений ---
async def send_message(chat_id: int, text: str):
    """Отправка сообщения пользователю"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    async with httpx.AsyncClient() as client:
        await client.post(url, json=payload)


@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    """Главный обработчик Telegram"""
    update = await request.json()
    logger.info(f"📩 Incoming Telegram update: {json.dumps(update, ensure_ascii=False)}")

    if "message" not in update:
        return {"ok": True}

    message = update["message"]
    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    # Обработка команд
    if text == "/start":
        await send_message(chat_id, "Привет! Я бот для записи через YCLIENTS 💅\n\n"
                                    "Доступные команды:\n"
                                    "• /services — список услуг\n"
                                    "• /help — помощь")
    elif text == "/services":
        services_text = await try_yclients_get_services()
        await send_message(chat_id, f"📋 Услуги:\n{services_text}")
    elif text == "/help":
        await send_message(chat_id, "Команды:\n"
                                    "/start — начать\n"
                                    "/services — показать список услуг\n"
                                    "/help — помощь")
    else:
        await send_message(chat_id, "Извини, я не понял 😅. Введи /help для списка команд.")

    return {"ok": True}


@app.get("/")
async def home():
    return {"status": "ok", "message": "Бот работает 🚀"}


# --- Запуск авторизации при старте ---
YCLIENTS_USER_TOKEN = yclients_auth()
if not YCLIENTS_USER_TOKEN:
    logger.error("⚠️ Не удалось авторизоваться в YCLIENTS. Проверь логин/пароль.")
else:
    logger.info("🔐 user_token получен успешно.")
