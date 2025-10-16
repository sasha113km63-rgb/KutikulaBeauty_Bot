import aiohttp
import logging
from config import (
    YCLIENTS_COMPANY_ID,
    YCLIENTS_PARTNER_TOKEN,
    YCLIENTS_LOGIN,
    YCLIENTS_PASSWORD,
)

logger = logging.getLogger("yclients_api")

BASE_URL = "https://api.yclients.com/api/v1"
user_token = None  # Кэш для токена пользователя


async def get_headers():
    """
    Возвращает корректные заголовки авторизации для YCLIENTS.
    """
    global user_token
    if not user_token:
        user_token = await get_user_token()

    headers = {
        "Accept": "application/vnd.yclients.v2+json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {YCLIENTS_PARTNER_TOKEN}",
        "User-Auth-Token": user_token or "",
    }
    return headers


async def get_user_token():
    """
    Получение user_token по логину и паролю (авторизация администратора).
    """
    url = f"{BASE_URL}/auth"
    data = {"login": YCLIENTS_LOGIN, "password": YCLIENTS_PASSWORD}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=data) as resp:
            result = await resp.json()
            if resp.status == 200 and result.get("data"):
                token = result["data"]["user_token"]
                logger.info(f"✅ Получен новый user_token: {token[:6]}...")
                return token
            else:
                logger.error(f"❌ Ошибка авторизации: {result}")
                return None


# --- Получение категорий услуг ---
async def get_categories():
    """
    Получить список категорий услуг компании.
    """
    url = f"{BASE_URL}/company/{YCLIENTS_COMPANY_ID}/service_categories"
    headers = await get_headers()

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            if data.get("success"):
                return data["data"]
            logger.error(f"Ошибка получения категорий: {data}")
            return []


# --- Получение услуг в категории ---
async def get_services_by_category(category_id):
    """
    Получить услуги конкретной категории.
    """
    url = f"{BASE_URL}/company/{YCLIENTS_COMPANY_ID}/services"
    headers = await get_headers()

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            if not data.get("success"):
                logger.error(f"Ошибка получения услуг: {data}")
                return []

            # Фильтруем по категории
            services = [s for s in data["data"] if s["category_id"] == category_id]
            return services


# --- Получение мастеров ---
async def get_masters_for_service(service_id):
    """
    Получить мастеров, предоставляющих конкретную услугу.
    """
    url = f"{BASE_URL}/company/{YCLIENTS_COMPANY_ID}/staff"
    headers = await get_headers()

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            if not data.get("success"):
                logger.error(f"Ошибка получения мастеров: {data}")
                return []

            masters = [m for m in data["data"] if service_id in m.get("services", [])]
            return masters


# --- Получение свободного времени ---
async def get_free_times(staff_id, service_id):
    """
    Получить список доступных временных слотов для записи.
    """
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
    """
    Создать клиента (если его ещё нет).
    """
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
    """
    Создать запись клиента.
    """
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
                logger.info(f"✅ Запись успешно создана: {data['data']['id']}")
                return data["data"]
            else:
                logger.error(f"Ошибка при создании записи: {data}")
                return None
