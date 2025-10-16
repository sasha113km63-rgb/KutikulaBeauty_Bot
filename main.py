import logging
from fastapi import FastAPI, Request
import asyncio
import telebot
from yclients_api import (
    get_categories,
    get_services_by_category,
    get_masters_for_service,
    get_free_times,
    create_booking,
)
from config import TELEGRAM_TOKEN

# --- Логирование ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

# --- FastAPI приложение ---
app = FastAPI()

# --- Telegram бот ---
bot = telebot.TeleBot(TELEGRAM_TOKEN)


@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = telebot.types.Update.de_json(data)
    bot.process_new_updates([update])
    return {"ok": True}


# --- Команда приветствия ---
@bot.message_handler(commands=["start"])
def start_message(message):
    bot.reply_to(
        message,
        "Здравствуйте!🌸\n"
        "Я — виртуальный администратор beauty studio KUTIKULA.\n"
        "Чем могу помочь? 💅",
    )
    show_categories(message.chat.id)


def show_categories(chat_id):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    categories = loop.run_until_complete(get_categories())

    if not categories:
        bot.send_message(chat_id, "❌ Не удалось загрузить категории.")
        return

    text = "Выберите категорию:\n\n"
    for c in categories:
        text += f"• {c['title']}\n"
    bot.send_message(chat_id, text)


# --- Тест ---
@app.get("/")
def root():
    return {"status": "bot running"}
