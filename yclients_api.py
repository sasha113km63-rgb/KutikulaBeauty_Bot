import os
import logging
import aiohttp
from typing import Any

logger = logging.getLogger("yclients_api")

# Базовый URL API.
BASE_URL = os.getenv("YCLIENTS_API_BASE", "").rstrip("/") or "https://api.yclients.com/api/v1"

# Авторизация (минимальная, как в вашем окружении)
YCLIENTS_USER_TOKEN = os.getenv("YCLIENTS_USER_TOKEN", "")
YCLIENTS_PARTNER_ID = os.getenv("YCLIENTS_PARTNER_ID", "")
YCLIENTS_PARTNER_TOKEN = os.getenv("YCLIENTS_PARTNER_TOKEN", "")

def get_headers() -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if YCLIENTS_USER_TOKEN:
        headers["Authorization"] = f"Bearer {YCLIENTS_USER_TOKEN}"
    if YCLIENTS_PARTNER_ID:
        headers["X-Partner-Id"] = str(YCLIENTS_PARTNER_ID)
    if YCLIENTS_PARTNER_TOKEN:
        headers["X-Partner-Token"] = str(YCLIENTS_PARTNER_TOKEN)
    return headers

async def _request(method: str, url: str, headers: dict, params: dict | None = None, json_data: Any | None = None) -> Any:
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, headers=headers, params=params, json=json_data) as resp:
            try:
                return await resp.json()
            except Exception:
                raw = await resp.text()
                logger.error(f"YCLIENTS non-json response ({resp.status}): {raw}")
                return {"success": False, "raw": raw, "status": resp.status}

def _extract_data_list(payload: Any) -> list[dict] | None:
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return data
    return None

# ---------------------------------------------------------------------
# Основное для уведомлений: получение записей (records)
# ---------------------------------------------------------------------
async def get_records(company_id: int, date_from: str, date_to: str) -> list[dict]:
    headers = get_headers()
    url = f"{BASE_URL}/records/{company_id}"

    params = {"start_date": date_from, "end_date": date_to, "count": 200, "page": 1}
    data = await _request("GET", url, headers, params=params)
    recs = _extract_data_list(data)
    if recs is not None:
        return recs

    # fallback POST (на некоторых настройках API так работает)
    payload = {"start_date": date_from, "end_date": date_to, "count": 200, "page": 1}
    data = await _request("POST", url, headers, json_data=payload)
    recs = _extract_data_list(data)
    return recs or []

# ---------------------------------------------------------------------
# Совместимость со старым main.py (запись через бота).
# Сейчас у вас запись через бота отключена, но старый main.py может
# пытаться импортировать эти функции. Чтобы деплой не падал, оставляем
# безопасные заглушки.
# ---------------------------------------------------------------------
async def get_categories(*args, **kwargs) -> list[dict]:
    logger.warning("get_categories() called, but booking flow is disabled / not implemented in this build.")
    return []

async def get_services_by_category(*args, **kwargs) -> list[dict]:
    logger.warning("get_services_by_category() called, but booking flow is disabled / not implemented in this build.")
    return []

async def get_masters_for_service(*args, **kwargs) -> list[dict]:
    logger.warning("get_masters_for_service() called, but booking flow is disabled / not implemented in this build.")
    return []

async def create_booking(*args, **kwargs) -> dict:
    logger.warning("create_booking() called, but booking flow is disabled / not implemented in this build.")
    return {"success": False, "message": "Booking via bot is disabled."}
