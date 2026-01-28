import os
import logging
import aiohttp
from typing import Any

logger = logging.getLogger("yclients_api")

BASE_URL = (os.getenv("YCLIENTS_API_BASE") or "https://api.yclients.com/api/v1").rstrip("/")

YCLIENTS_USER_TOKEN = os.getenv("YCLIENTS_USER_TOKEN", "")
YCLIENTS_PARTNER_ID = os.getenv("YCLIENTS_PARTNER_ID", "")
YCLIENTS_PARTNER_TOKEN = os.getenv("YCLIENTS_PARTNER_TOKEN", "")

def get_headers() -> dict:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
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
                logger.error(f"YCLIENTS non-json response status={resp.status}: {raw}")
                return {"success": False, "status": resp.status, "raw": raw}

def _unwrap(resp_json: Any) -> Any:
    if not isinstance(resp_json, dict):
        return resp_json
    if "data" in resp_json:
        return resp_json["data"]
    if isinstance(resp_json.get("data"), dict) and "data" in resp_json["data"]:
        return resp_json["data"]["data"]
    return resp_json

def _as_record(obj: Any) -> dict | None:
    obj = _unwrap(obj)
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        return obj[0]
    return None

# ---- Заглушки старого функционала (чтобы imports не ломались) ----
async def get_categories(*args, **kwargs):
    return []

async def get_services_by_category(*args, **kwargs):
    return []

async def get_masters_for_service(*args, **kwargs):
    return []

async def create_booking(*args, **kwargs):
    return {"success": False, "error": "booking disabled"}

# ---- НОВОЕ: получить запись по id (для webhook) ----
async def get_record_by_id(company_id: int, record_id: str) -> dict | None:
    headers = get_headers()
    rid = str(record_id).strip()
    if not rid:
        return None

    candidates = [
        f"{BASE_URL}/records/{company_id}/{rid}",
        f"{BASE_URL}/record/{company_id}/{rid}",
        f"{BASE_URL}/records/{rid}",
        f"{BASE_URL}/record/{rid}",
    ]

    for url in candidates:
        try:
            resp = await _request("GET", url, headers=headers)
            rec = _as_record(resp)
            if rec:
                return rec
        except Exception as e:
            logger.error(f"get_record_by_id error {url}: {e}")

    return None
