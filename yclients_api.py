import os
import json
import logging
import aiohttp
from fastapi import FastAPI

# --- Настройка логирования ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kutikula_bot")

# --- Основное приложение ---
app = FastAPI()

# --- Конфигурация ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
YCLIENTS_PARTNER_TOKEN = os.getenv("YCLIENTS_PARTNER_TOKEN")
YCLIENTS_LOGIN = os.getenv("YCLIENTS_LOGIN")
YCLIENTS_PASSWORD = os.getenv("YCLIENTS_PASSWORD")
YCLIENTS_COMPANY_ID = os.getenv("YCLIENTS_COMPANY_ID", "530777")  # <-- добавлено

BASE_URL = "https://api.yclients.com/api/v1"  # <-- добавлено


# --- Импорт уведомлений (файл notifications.py) ---
from notifications import (
    send_new_booking_notification,
    send_cancel_notification,
    send_bonus_notification,
)


# --- Глобальный токен пользователя YCLIENTS ---
user_token = None


# --- Авторизация и получение user_token ---
async def get_user_token():
    """Авторизация администратора в YCLIENTS и получение user_token"""
    global user_token
    logger.info(f"🔐 Авторизация в YCLIENTS с логином: {YCLIENTS_LOGIN}")
    url = f"{BASE_URL}/auth"
    payload = {"login": YCLIENTS_LOGIN, "password": YCLIENTS_PASSWORD}
    headers = {
        "Accept": "application/vnd.yclients.v2+json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {YCLIENTS_PARTNER_TOKEN}"
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            result = await resp.json()
            if result.get("success") and result.get("data"):
                user_token = result["data"]["user_token"]
                logger.info(f"✅ Авторизация успешна. user_token: {user_token[:8]}...")
                return user_token
            else:
                logger.error(f"❌ Ошибка авторизации: {result}")
                return None


# --- Заголовки авторизации ---
async def get_headers():
    """
    Возвращает корректные заголовки авторизации для всех запросов YCLIENTS.
    """
    global user_token
    if not user_token:
        user_token = await get_user_token()

    headers = {
        "Accept": "application/vnd.yclients.v2+json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {YCLIENTS_PARTNER_TOKEN}, User {user_token}"
    }

    logger.info(f"✅ Заголовки сформированы корректно.")
    return headers


# --- Получение категорий услуг ---
async def get_categories():
    url = f"{BASE_URL}/company/{YCLIENTS_COMPANY_ID}/service_categories"
    headers = await get_headers()

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            if data.get("success"):
                return data["data"]
            logger.error(f"Ошибка получения категорий: {data}")
            return []


# --- Получение услуг по категории ---
async def get_services_by_category(category_id):
    url = f"{BASE_URL}/company/{YCLIENTS_COMPANY_ID}/services"
    headers = await get_headers()

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            if not data.get("success"):
                logger.error(f"Ошибка получения услуг: {data}")
                return []

            return [s for s in data["data"] if s["category_id"] == category_id]


# --- Получение мастеров ---
async def get_masters_for_service(service_id):
    url = f"{BASE_URL}/company/{YCLIENTS_COMPANY_ID}/staff"
    headers = await get_headers()

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            if not data.get("success"):
                logger.error(f"Ошибка получения мастеров: {data}")
                return []

            masters = []
            for m in data["data"]:
                service_ids = [s["id"] for s in m.get("services", [])]
                if service_id in service_ids:
                    masters.append(m)
            return masters


# --- Получение свободного времени ---
async def get_free_times(staff_id, service_id):
    url = f"{BASE_URL}/book_times/{YCLIENTS_COMPANY_ID}/{staff_id}/{service_id}"
    headers = await get_headers()

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            if not data.get("success"):
                logger.error(f"Ошибка получения свободного времени: {data}")
                return []

            times = []
            for day in data["data"]:
                for time in day["times"]:
                    times.append(f"{day['date']} {time}")
            return times


# --- Создание клиента ---
async def create_client(name, last_name, phone):
    url = f"{BASE_URL}/company/{YCLIENTS_COMPANY_ID}/clients"
    headers = await get_headers()
    payload = {"name": f"{name} {last_name}", "phone": phone}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            data = await resp.json()
            if data.get("success"):
                logger.info(f"✅ Клиент создан/обновлён: {phone}")
                return data["data"]
            else:
                logger.error(f"Ошибка создания клиента: {data}")
                return None


# --- Создание записи ---
async def create_booking(name, last_name, phone, service_id, master_id, time):
    client = await create_client(name, last_name, phone)
    if not client:
        return None

    url = f"{BASE_URL}/book_record/{YCLIENTS_COMPANY_ID}"
    headers = await get_headers()
    payload = {
        "staff_id": master_id,
        "services": [{"id": service_id}],
        "client": {
            "id": client["id"],
            "name": client["name"],
            "phone": client["phone"],
        },
        "datetime": time,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            data = await resp.json()
            if data.get("success"):
                booking = data["data"]
                logger.info(f"✅ Запись успешно создана: {booking['id']}")

                # --- Отправляем уведомление клиенту ---
                client_info = {
                    "name": client["name"],
                    "telegram_id": client.get("telegram_id", None),  # позже можно будет подставлять
                }
                booking_info = {
                    "service_name": booking["services"][0]["title"] if booking.get("services") else "Услуга",
                    "day_month": booking["datetime"].split(" ")[0],
                    "start_time": booking["datetime"].split(" ")[1],
                    "staff_name": booking.get("staff", {}).get("name", "Мастер"),
                    "price": booking["services"][0].get("cost", "—") if booking.get("services") else "—",
                }

                # Отправляем сообщение клиенту в Telegram
                try:
                    await send_new_booking_notification(client_info, booking_info)
                except Exception as e:
                    logger.error(f"Ошибка при отправке уведомления: {e}")

                 return booking
            else:
                logger.error(f"Ошибка при создании записи: {data}")
                return None
