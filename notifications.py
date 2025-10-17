import aiohttp
import logging
from config import TELEGRAM_TOKEN

logger = logging.getLogger("notifications")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"


# === 📌 Универсальная функция отправки сообщения ===
async def send_message(chat_id: int, text: str):
    """Отправка сообщения клиенту в Telegram"""
    async with aiohttp.ClientSession() as session:
        await session.post(
            TELEGRAM_API_URL,
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        )
    logger.info(f"📨 Сообщение отправлено пользователю {chat_id}")


# === 🧩 Форматирование текста с подстановкой данных ===
def format_message(template: str, data: dict) -> str:
    """
    Подставляет данные (имя, дата, услуга и т.п.) в шаблон текста.
    Пример: "{name}, вы записаны на {service}" → "Анна, вы записаны на маникюр"
    """
    for key, value in data.items():
        template = template.replace(f"{{{key}}}", str(value))
    return template


# === 🧾 ШАБЛОНЫ СООБЩЕНИЙ ===
TEMPLATES = {
    "new_booking": (
        "Здравствуйте, {name}!🌸\n\n"
        "Вы успешно записаны на <b>{service}</b> в студии <b>KUTIKULA</b> 💅\n"
        "📅 Дата: {day_month}\n"
        "🕒 Время: {start_time}\n"
        "👩‍🎨 Мастер: {staff}\n"
        "💰 Стоимость: {price} ₽\n\n"
        "Мы будем ждать вас по адресу: Казань, ул. Академика Королева, 26."
    ),
    "cancel_booking": (
        "⚠️ {name}, ваша запись на {service} ({day_month}, {start_time}) была отменена.\n"
        "Если хотите перенести — просто напишите мне 💬"
    ),
    "bonus": (
        "Спасибо, что выбрали нас, {name}!💖\n"
        "Вам начислен бонус за посещение {service} — {bonus_points} баллов 🎁"
    ),
}


# === ✨ Отправка сообщений по событию ===
async def send_new_booking_notification(client, booking):
    """Сообщение при создании новой записи"""
    text = format_message(
        TEMPLATES["new_booking"],
        {
            "name": client.get("name"),
            "service": booking.get("service_name"),
            "day_month": booking.get("day_month"),
            "start_time": booking.get("start_time"),
            "staff": booking.get("staff_name"),
            "price": booking.get("price", "—"),
        },
    )
    await send_message(client["telegram_id"], text)


async def send_cancel_notification(client, booking):
    """Сообщение при отмене записи"""
    text = format_message(
        TEMPLATES["cancel_booking"],
        {
            "name": client.get("name"),
            "service": booking.get("service_name"),
            "day_month": booking.get("day_month"),
            "start_time": booking.get("start_time"),
        },
    )
    await send_message(client["telegram_id"], text)


async def send_bonus_notification(client, booking):
    """Сообщение с бонусами после посещения"""
    text = format_message(
        TEMPLATES["bonus"],
        {
            "name": client.get("name"),
            "service": booking.get("service_name"),
            "bonus_points": booking.get("bonus_points", 50),
        },
    )
    await send_message(client["telegram_id"], text)
