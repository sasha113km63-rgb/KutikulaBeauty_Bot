import json
import os
import asyncio

FILE_PATH = "dialog_memory.json"
_lock = asyncio.Lock()


def _read_file() -> dict:
    if not os.path.exists(FILE_PATH):
        return {}
    try:
        with open(FILE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_file(data: dict) -> None:
    with open(FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


async def upsert_user(tg_id: int, name: str | None = None):
    # Без базы — просто заглушка (можно расширить позже)
    return


async def get_state(tg_id: int) -> tuple[str, dict]:
    async with _lock:
        data = _read_file()
        st = data.get(str(tg_id), {})
        step = st.get("step", "idle")
        payload = st.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        return step, payload


async def set_state(tg_id: int, step: str, payload: dict):
    async with _lock:
        data = _read_file()
        data[str(tg_id)] = {"step": step, "payload": payload}
        _write_file(data)


async def reset_state(tg_id: int):
    await set_state(tg_id, "idle", {})
