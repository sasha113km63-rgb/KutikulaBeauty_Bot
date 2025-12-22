import os
import logging
import aiohttp

# ------------------- ЛОГИ -------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kutikula_bot")

# ------------------- ENV -------------------
YCLIENTS_PARTNER_TOKEN = os.getenv("YCLIENTS_PARTNER_TOKEN")
YCLIENTS_LOGIN = os.getenv("YCLIENTS_LOGIN")
YCLIENTS_PASSWORD = os.getenv("YCLIENTS_PASSWORD")

# В Render ты уже ставила это в переменных окружения
YCLIENTS_COMPANY_ID = os.getenv("YCLIENTS_COMPANY_ID", "530777")

# Можно хранить в ENV, а можно оставить дефолт
BASE_URL = os.getenv("YCLIENTS_API_BASE", "https://api.yclients.com/api/v1")

# ------------------- КЭШ user_token -------------------
_user_token: str | None = None


async def get_user_token() -> str | None:
    """
    Авторизация администратора в YCLIENTS (логин/пароль) и получение user_token.
    """
    global _user_token

    if not YCLIENTS_LOGIN or not YCLIENTS_PASSWORD or not YCLIENTS_PARTNER_TOKEN:
        logger.error("❌ Не хватает переменных окружения: YCLIENTS_LOGIN / YCLIENTS_PASSWORD / YCLIENTS_PARTNER_TOKEN")
        return None

    url = f"{BASE_URL}/auth"
    payload = {"login": YCLIENTS_LOGIN, "password": YCLIENTS_PASSWORD}
    headers = {
        "Accept": "application/vnd.yclients.v2+json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {YCLIENTS_PARTNER_TOKEN}",
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            data = await resp.json()

    if isinstance(data, dict) and data.get("success") and data.get("data"):
        _user_token = data["data"].get("user_token")
        if _user_token:
            logger.info(f"✅ Авторизация успешна. user_token: {_user_token[:8]}...")
            return _user_token

    logger.error(f"❌ Ошибка авторизации: {data}")
    return None


async def get_headers() -> dict:
    """
    Заголовки для запросов YCLIENTS.
    """
    global _user_token

    if not _user_token:
        _user_token = await get_user_token()

    if not _user_token:
        # если токен не получили — вернем заголовки с партнерским (чтобы хотя бы лог был понятный)
        return {
            "Accept": "application/vnd.yclients.v2+json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {YCLIENTS_PARTNER_TOKEN}",
        }

    return {
        "Accept": "application/vnd.yclients.v2+json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {YCLIENTS_PARTNER_TOKEN}, User {_user_token}",
    }


# ------------------- СПРАВОЧНИКИ -------------------
async def get_categories():
    url = f"{BASE_URL}/company/{YCLIENTS_COMPANY_ID}/service_categories"
    headers = await get_headers()

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()

    if isinstance(data, dict) and data.get("success"):
        return data.get("data", [])

    logger.error(f"❌ Ошибка получения категорий: {data}")
    return []


async def get_services_by_category(category_id: int):
    """
    Берем все услуги и фильтруем по category_id.
    """
    url = f"{BASE_URL}/company/{YCLIENTS_COMPANY_ID}/services"
    headers = await get_headers()

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()

    if not (isinstance(data, dict) and data.get("success")):
        logger.error(f"❌ Ошибка получения услуг: {data}")
        return []

    services = data.get("data", [])
    return [s for s in services if s.get("category_id") == category_id]


async def get_masters_for_service(service_id: int):
    """
    Получаем всех сотрудников и фильтруем тех, у кого услуга есть в списке services.
    """
    url = f"{BASE_URL}/company/{YCLIENTS_COMPANY_ID}/staff"
    headers = await get_headers()

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()

    if not (isinstance(data, dict) and data.get("success")):
        logger.error(f"❌ Ошибка получения мастеров: {data}")
        return []

    masters = []
    for m in data.get("data", []):
        service_ids = [s.get("id") for s in m.get("services", []) if isinstance(s, dict)]
        if service_id in service_ids:
            masters.append(m)

    return masters


# ------------------- ВРЕМЯ (book_times) -------------------
async def get_free_times_for_date(staff_id: int, service_id: int, day_iso: str):
    """
    Получаем свободное время на дату.
    day_iso: "YYYY-MM-DD"
    Возвращаем список строк "YYYY-MM-DD HH:MM"
    """
    headers = await get_headers()

    # правильный формат:
    # /book_times/{company_id}/{staff_id}/{date}
    url = f"{BASE_URL}/book_times/{YCLIENTS_COMPANY_ID}/{staff_id}/{day_iso}"

    # иногда у филиалов требуется фильтр по услуге — пробуем несколько вариантов параметров
    params_variants = [
        {"service_ids[]": str(service_id)},
        {"service_ids": str(service_id)},
        None,
    ]

    async with aiohttp.ClientSession() as session:
        for params in params_variants:
            try:
                async with session.get(url, headers=headers, params=params) as resp:
                    data = await resp.json()
            except Exception as e:
                logger.error(f"❌ Ошибка запроса времени: {e}")
                continue

            # если success=False — пробуем следующий вариант params
            if isinstance(data, dict) and data.get("success") is False:
                logger.error(f"⚠️ book_times неуспешно: {data}")
                continue

            if not isinstance(data, dict) or "data" not in data:
                logger.error(f"⚠️ Неожиданный ответ book_times: {data}")
                continue

            items = data.get("data")

            # формат 1: ["10:00","10:30",...]
            if isinstance(items, list) and items and isinstance(items[0], str):
                return [f"{day_iso} {t}" for t in items]

            # формат 2: [{"time":"10:00"}, ...]
            if isinstance(items, list):
                times = []
                for it in items:
                    if isinstance(it, dict):
                        t = it.get("time") or it.get("datetime") or it.get("start")
                        if t:
                            times.append(str(t))

                if times and times[0].startswith(day_iso):
                    return times

                if times:
                    return [f"{day_iso} {t}" for t in times]

                return []

    return []


# ------------------- КЛИЕНТ + ЗАПИСЬ -------------------
async def create_client(name: str, last_name: str, phone: str):
    """
    Создаёт или обновляет клиента. Иногда YCLIENTS возвращает data как список — мы это учитываем.
    """
    url = f"{BASE_URL}/company/{YCLIENTS_COMPANY_ID}/clients"
    headers = await get_headers()

    payload = {
        "name": f"{name} {last_name}".strip(),
        "phone": phone,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            data = await resp.json()

    if not isinstance(data, dict) or not data.get("success"):
        logger.error(f"❌ Ошибка создания клиента: {data}")
        return None

    client_data = data.get("data")

    # Иногда data = [ {...} ]
    if isinstance(client_data, list):
        if not client_data:
            logger.error(f"❌ Пустой список клиента: {data}")
            return None
        client_data = client_data[0]

    if not isinstance(client_data, dict) or "id" not in client_data:
        logger.error(f"❌ Неожиданный формат клиента: {client_data}")
        return None

    logger.info(f"✅ Клиент создан/обновлён: {phone}")
    return client_data


async def create_booking(name: str, last_name: str, phone: str, service_id: int, master_id: int, time: str):
    """
    Создаёт запись.
    time: "YYYY-MM-DD HH:MM"
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
            "name": client.get("name", f"{name} {last_name}".strip()),
            "phone": client.get("phone", phone),
        },
        "datetime": time,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            data = await resp.json()

    if not isinstance(data, dict) or not data.get("success"):
        logger.error(f"❌ Ошибка при создании записи: {data}")
        return None

    booking = data.get("data")

    # Иногда data = [ {...} ]
    if isinstance(booking, list):
        booking = booking[0] if booking else None

    if not booking:
        logger.error(f"❌ Пустые данные записи: {data}")
        return None

    logger.info(f"✅ Запись создана: {booking}")
    return booking
