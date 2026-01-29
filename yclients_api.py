import os
import logging
import aiohttp
from typing import Any

logger = logging.getLogger("yclients_api")

# Базовый URL API.
BASE_URL = os.getenv("YCLIENTS_API_BASE", "").rstrip("/") or "https://api.yclients.com/api/v1"

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
                data = await resp.json()
            except Exception:
                raw = await resp.text()
                logger.error(f"YCLIENTS non-json response: {raw}")
                return {"success": False, "raw": raw, "status": resp.status}
            return data

def _extract_data_list(resp_json: Any) -> list[dict] | None:
    if not isinstance(resp_json, dict):
        return None
    if "data" in resp_json and isinstance(resp_json["data"], list):
        return resp_json["data"]
    if "data" in resp_json and isinstance(resp_json["data"], dict) and isinstance(resp_json["data"].get("data"), list):
        return resp_json["data"]["data"]
    return None

def _extract_data_dict(resp_json: Any) -> dict | None:
    if not isinstance(resp_json, dict):
        return None
    if "data" in resp_json and isinstance(resp_json["data"], dict):
        return resp_json["data"]
    if "data" in resp_json and isinstance(resp_json["data"], dict) and isinstance(resp_json["data"].get("data"), dict):
        return resp_json["data"]["data"]
    return None

# ---------------------------------------------------------------------
# ВНИМАНИЕ: функции ниже (get_categories и т.п.) оставлены заглушками
# для совместимости со старым main.py. В вашей текущей задаче они не нужны.
# ---------------------------------------------------------------------
async def get_categories(*args, **kwargs):
    return []

async def get_services_by_category(*args, **kwargs):
    return []

async def get_masters_for_service(*args, **kwargs):
    return []

async def create_booking(*args, **kwargs):
    return {"success": False, "error": "booking disabled"}

# ---------------------------------------------------------------------
# Получить запись по id (для webhook, чтобы вытащить телефон/услугу)
# ---------------------------------------------------------------------
async def get_record_by_id(company_id: int, record_id: str) -> dict | None:
    headers = get_headers()
    rid = str(record_id).strip()
    if not rid:
        return None

    candidates = [
        f"{BASE_URL}/record/{company_id}/{rid}",
        f"{BASE_URL}/records/{company_id}/{rid}",
        f"{BASE_URL}/record/{rid}",
        f"{BASE_URL}/records/{rid}",
    ]

    for url in candidates:
        try:
            data = await _request("GET", url, headers)
            rec = _extract_data_dict(data)
            if rec is not None:
                return rec
        except Exception as e:
            logger.error(f"get_record_by_id error {url}: {e}")

    return None
