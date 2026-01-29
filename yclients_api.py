import os
import logging
import aiohttp
from typing import Any, Optional

logger = logging.getLogger("yclients_api")

BASE_URL = (os.getenv("YCLIENTS_API_BASE") or "https://api.yclients.com/api/v1").rstrip("/")

YCLIENTS_USER_TOKEN = os.getenv("YCLIENTS_USER_TOKEN", "")
YCLIENTS_PARTNER_ID = os.getenv("YCLIENTS_PARTNER_ID", "")
YCLIENTS_PARTNER_TOKEN = os.getenv("YCLIENTS_PARTNER_TOKEN", "")

def get_headers() -> dict:
    """
    Заголовки для YCLIENTS API.
    Важно: токены/партнёрские заголовки должны быть заданы в ENV.
    """
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
        async with session.request(method, url, headers=headers, params=params, json=json_data, timeout=25) as resp:
            try:
                return await resp.json()
            except Exception:
                raw = await resp.text()
                logger.error(f"YCLIENTS non-json response status={resp.status}: {raw}")
                return {"success": False, "status": resp.status, "raw": raw}

def _extract_data_dict(resp_json: Any) -> Optional[dict]:
    """
    YCLIENTS часто возвращает {"success": true, "data": {...}} или похожие структуры.
    """
    if not isinstance(resp_json, dict):
        return None
    if isinstance(resp_json.get("data"), dict):
        return resp_json["data"]
    # иногда бывает вложение data.data
    if isinstance(resp_json.get("data"), dict) and isinstance(resp_json["data"].get("data"), dict):
        return resp_json["data"]["data"]
    return None

# ---- СТАРЫЕ ФУНКЦИИ (если они у тебя были в проекте) ----
async def get_records(company_id: int) -> list:
    """
    Вернёт список записей (ограничено тем, что отдаёт API).
    Если тебе не нужно — можешь не использовать.
    """
    url = f"{BASE_URL}/records/{company_id}"
    headers = get_headers()
    resp = await _request("GET", url, headers=headers)
    if isinstance(resp, dict) and isinstance(resp.get("data"), list):
        return resp["data"]
    if isinstance(resp, dict) and isinstance(resp.get("data"), dict) and isinstance(resp["data"].get("data"), list):
        return resp["data"]["data"]
    return []

# ---- НОВОЕ: получить запись по id (для webhook) ----
async def get_record_by_id(company_id: int, record_id: str) -> Optional[dict]:
    """
    Пытаемся найти корректный endpoint, потому что у разных токенов/доступов
    и версий API могут работать разные URL.
    """
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
            rec = _extract_data_dict(resp)
            if rec:
                return rec
        except Exception as e:
            logger.error(f"get_record_by_id error {url}: {e}")

    return None
