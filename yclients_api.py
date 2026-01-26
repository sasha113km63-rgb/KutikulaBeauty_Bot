import os
import logging
import aiohttp
from typing import Any

logger = logging.getLogger("yclients_api")

# Базовый URL API.
# Если у вас уже был YCLIENTS_API_BASE в env — используем его.
# Иначе оставляем дефолт (может отличаться в вашей интеграции — при необходимости замените).
BASE_URL = os.getenv("YCLIENTS_API_BASE", "").rstrip("/") or "https://api.yclients.com/api/v1"

# Авторизация: в разных аккаунтах YCLIENTS используются разные схемы.
# Мы пробуем собрать заголовки из доступных переменных окружения.
YCLIENTS_USER_TOKEN = os.getenv("YCLIENTS_USER_TOKEN", "")
YCLIENTS_PARTNER_ID = os.getenv("YCLIENTS_PARTNER_ID", "")
YCLIENTS_PARTNER_TOKEN = os.getenv("YCLIENTS_PARTNER_TOKEN", "")
YCLIENTS_LOGIN = os.getenv("YCLIENTS_LOGIN", "")
YCLIENTS_PASSWORD = os.getenv("YCLIENTS_PASSWORD", "")

def get_headers() -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    # Самый частый вариант: Bearer user token
    if YCLIENTS_USER_TOKEN:
        headers["Authorization"] = f"Bearer {YCLIENTS_USER_TOKEN}"

    # Некоторые партнерские интеграции требуют дополнительных заголовков
    if YCLIENTS_PARTNER_ID:
        headers["X-Partner-Id"] = str(YCLIENTS_PARTNER_ID)
    if YCLIENTS_PARTNER_TOKEN:
        headers["X-Partner-Token"] = str(YCLIENTS_PARTNER_TOKEN)

    return headers

async def _request(method: str, url: str, headers: dict, params: dict | None = None, json_data: Any | None = None) -> Any:
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, headers=headers, params=params, json=json_data) as resp:
            try:
                data = await resp.json()
            except Exception:
                raw = await resp.text()
                logger.error(f"YCLIENTS non-json response: {raw}")
                return {"success": False, "raw": raw, "status": resp.status}

            # Иногда API отвечает {"success":true,"data":...} или {"ok":true,"data":...}
            return data

# ---------------------------------------------------------------------
# Для текущей задачи нам критично получить записи (records).
# ---------------------------------------------------------------------
async def get_records(company_id: int, date_from: str, date_to: str) -> list[dict]:
    """
    Получает список записей за период [date_from, date_to].
    date_from/date_to: 'YYYY-MM-DD'

    Реализация сделана максимально "живучей":
    - сначала пробуем GET /records/{company_id} с параметрами
    - если не получилось — пробуем POST /records/{company_id} с json телом
    """
    headers = get_headers()
    url = f"{BASE_URL}/records/{company_id}"

    params = {
        "start_date": date_from,
        "end_date": date_to,
        "count": 200,
        "page": 1,
    }

    data = await _request("GET", url, headers, params=params)
    recs = _extract_data_list(data)
    if recs is not None:
        return recs

    # fallback POST
    payload = {
        "start_date": date_from,
        "end_date": date_to,
        "count": 200,
        "page": 1,
    }
    data2 = await _request("POST", url, headers, json_data=payload)
    recs2 = _extract_data_list(data2)
    if recs2 is not None:
        return recs2

    logger.error(f"Не удалось получить records: {data} / {data2}")
    return []

def _extract_data_list(resp_json: Any) -> list[dict] | None:
    if not isinstance(resp_json, dict):
        return None
    if "data" in resp_json and isinstance(resp_json["data"], list):
        return resp_json["data"]
    # иногда API может вернуть {"success":true,"data":{"data":[...]}}
    if "data" in resp_json and isinstance(resp_json["data"], dict) and isinstance(resp_json["data"].get("data"), list):
        return resp_json["data"]["data"]
    return None
