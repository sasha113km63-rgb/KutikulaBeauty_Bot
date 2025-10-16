import logging
import aiohttp
import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from config import TELEGRAM_TOKEN
from yclients_api import (
    get_categories,
    get_services_by_category,
    get_masters_for_service,
    get_free_times,
    create_booking,
)

# --- Логирование ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

# --- Telegram API ---
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# --- FastAPI ---
app = FastAPI()


async def send_message(chat_id: int, text: str):
    """Отправка сообщения пользователю через Telegram API"""
    async with aiohttp.ClientSession() as session:
        await session.post(f"{TELEGRAM_API_URL}/sendMessage", json={"chat_id": chat_id, "text": text})


@app.get("/")
async def root():
    return {"status": "ok", "message": "Kutikula bot is running"}


@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    """Обработка входящих сообщений от Telegram"""
    update = await request.json()
    logger.info(f"📩 Incoming update: {update}")

    message = update.get("message")
    if not message:
        return JSONResponse(content={"ok": True})

    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip().lower()

    # --- Обработка приветствия ---
    greetings = ["привет", "здравствуйте", "добрый день", "доброе утро", "добрый вечер", "hi", "hello", "/start"]
    if any(word in text for word in greetings):
        reply = (
            "Здравствуйте!🌸\n"
            "Я — виртуальный администратор *beauty studio KUTIKULA* 💅\n\n"
            "Чем могу помочь?\n"
            "▫ Записаться на процедуру\n"
            "▫ Посмотреть услуги\n"
            "▫ Узнать расписание мастеров"
        )
        await send_message(chat_id, reply)

        # Показ категорий
        categories = await get_categories()
        if not categories:
            await send_message(chat_id, "❌ Не удалось загрузить категории услуг.")
        else:
            msg = "Выберите категорию услуг:\n\n"
            for c in categories:
                msg += f"• {c['title']}\n"
            await send_message(chat_id, msg)

        return JSONResponse(content={"ok": True})

   @app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    """Обработка входящих сообщений от Telegram"""
    update = await request.json()
    logger.info(f"📩 Incoming update: {update}")

    message = update.get("message")
    if not message:
        return JSONResponse(content={"ok": True})

    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip().lower()

    # --- Обработка приветствия ---
    greetings = ["привет", "здравствуйте", "добрый день", "доброе утро", "добрый вечер", "hi", "hello", "/start"]
    if any(word in text for word in greetings):
        reply = (
            "Здравствуйте!🌸\n"
            "Я — виртуальный администратор *beauty studio KUTIKULA* 💅\n\n"
            "Чем могу помочь?\n"
            "▫ Записаться на процедуру\n"
            "▫ Посмотреть услуги\n"
            "▫ Узнать расписание мастеров"
        )
        await send_message(chat_id, reply)

        # Показ категорий
        categories = await get_categories()
        if not categories:
            await send_message(chat_id, "❌ Не удалось загрузить категории услуг.")
        else:
            msg = "Выберите категорию услуг:\n\n"
            for c in categories:
                msg += f"• {c['title']}\n"
            await send_message(chat_id, msg)

        return JSONResponse(content={"ok": True})

    # --- Обработка выбора категории ---
    categories = await get_categories()
    if categories:
        selected = next((c for c in categories if c["title"].lower() == text), None)
        if selected:
            await send_message(chat_id, f"📋 Отлично! Вы выбрали категорию: {selected['title']}\nЗагружаю услуги...")

            services = await get_services_by_category(selected["id"])
            if not services:
                await send_message(chat_id, "❌ Не удалось получить услуги этой категории.")
            else:
                msg = f"💅 *Услуги в категории {selected['title']}:*\n\n"
                for s in services:
                    price = s.get("price_min") or s.get("price") or 0
                    msg += f"• {s['title']} — {price} ₽\n"
                await send_message(chat_id, msg)
            return JSONResponse(content={"ok": True})

    # --- Неизвестное сообщение ---
    await send_message(chat_id, "Извините, я вас не поняла 😅. Напишите «привет» для начала.")
    return JSONResponse(content={"ok": True})
