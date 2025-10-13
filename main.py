import os
import logging
import telebot
from telebot import types
from yclients_api import authorize_user, get_records, delete_record

# Настройки логирования
logging.basicConfig(level=logging.INFO)

# Загружаем токен бота
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

bot = telebot.TeleBot(TELEGRAM_TOKEN)


# --------------------- Команды ---------------------

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(
        message,
        "👋 Привет! Я бот для управления записями YCLIENTS.\n\n"
        "Доступные команды:\n"
        "/myrecords — посмотреть твои записи\n"
        "/cancel — отменить запись"
    )


@bot.message_handler(commands=['myrecords'])
def my_records(message):
    """Показать все записи пользователя"""
    bot.send_message(message.chat.id, "🔄 Получаю твои записи, подожди...")

    records_data = get_records()

    if not records_data or not records_data.get("data"):
        bot.send_message(message.chat.id, "😕 У тебя пока нет активных записей.")
        return

    for record in records_data["data"]:
        service_name = record["services"][0]["title"] if record.get("services") else "Без названия"
        staff_name = record["staff"]["name"] if record.get("staff") else "Без мастера"
        datetime = record["datetime"]
        record_id = record["id"]
        record_hash = record["record_hash"]

        text = (
            f"💅 <b>{service_name}</b>\n"
            f"👩‍🎨 Мастер: {staff_name}\n"
            f"📅 Дата: {datetime}\n"
        )

        # Кнопка отмены
        keyboard = types.InlineKeyboardMarkup()
        cancel_btn = types.InlineKeyboardButton(
            text="❌ Отменить запись",
            callback_data=f"cancel_{record_id}_{record_hash}"
        )
        keyboard.add(cancel_btn)

        bot.send_message(
            message.chat.id,
            text,
            parse_mode="HTML",
            reply_markup=keyboard
        )


@bot.callback_query_handler(func=lambda call: call.data.startswith("cancel_"))
def cancel_record(call):
    """Удаление записи"""
    _, record_id, record_hash = call.data.split("_")

    bot.answer_callback_query(call.id, "Удаляю запись...")
    response = delete_record(record_id, record_hash)

    if response.get("success") or response.get("meta"):
        bot.edit_message_text(
            "✅ Запись успешно удалена.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id
        )
    else:
        bot.send_message(
            call.message.chat.id,
            f"⚠️ Ошибка при удалении: {response}"
        )


# --------------------- Запуск ---------------------

if name == "__main__":
    authorize_user()  # Авторизация при старте
    bot.polling(none_stop=True)
