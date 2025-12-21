import logging
import aiohttp
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from config import TELEGRAM_TOKEN
from db import init_db
from storage import upsert_user, get_state, set_state, reset_state
from keyboards import inline_keyboard
from yclients_api import (
    get_categories,
    get_services_by_category,
    get_masters_for_service,
)

# --- Логирование ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

# --- Telegram API ---
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# --- FastAPI ---
app = FastAPI()


# ---------- Telegram helpers ----------

async def tg_post(method: str, payload: dict):
    async with aiohttp.ClientSession() as session:
        await session.post(f"{TELEGRAM_API_URL}/{method}", json=payload)


async def send_message(chat_id: int, text: str, reply_markup: dict | None = None):
    payload = {
        "chat_id": chat_id,
        "text": text,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    await tg_post("sendMessage", payload)


async def answer_callback(callback_query_id: str):
    await tg_post("answerCallbackQuery", {"callback_query_id": callback_query_id})


# ---------- App lifecycle ----------

@app.on_event("startup")
async def startup():
    await init_db()
    logger.info("DB initialized")


@app.get("/")
async def root():
    return {"status": "ok", "message": "KutikulaBeautyBot is running"}


# ---------- Telegram webhook (ОДИН!) ----------

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    logger.info(f"Incoming update: {update}")

    # ====== ОБРАБОТКА КНОПОК ======
    if "callback_query" in update:
        cq = update["callback_query"]
        data = cq.get("data", "")
        chat_id = cq["message"]["chat"]["id"]

        await answer_callback(cq["id"])

        # --- Выбор категории ---
        if data.startswith("cat:"):
            category_id = int(data.split(":")[1])
            await set_state(chat_id, "choose_service", {"category_id": category_id})

            services = await get_services_by_category(category_id)
            if not services:
                await send_message(chat_id, "Не удалось загрузить услуги 😔")
                return JSONResponse(content={"ok": True})

            buttons = [(s["title"], f"svc:{s['id']}") for s in services[:30]]
            await send_message(chat_id, "Выберите услугу:", inline_keyboard(buttons, row=1))
            return JSONResponse(content={"ok": True})

        # --- Выбор услуги ---
        if data.startswith("svc:"):
            service_id = int(data.split(":")[1])
            step, payload = await get_state(chat_id)
            payload["service_id"] = service_id
            await set_state(chat_id, "choose_master", payload)

            masters = await get_masters_for_service(service_id)
            if not masters:
                await send_message(chat_id, "По этой услуге нет мастеров 😔")
                return JSONResponse(content={"ok": True})

            buttons = [(m["name"], f"mst:{m['id']}") for m in masters[:30]]
            await send_message(chat_id, "Выберите мастера:", inline_keyboard(buttons, row=1))
            return JSONResponse(content={"ok": True})

        # --- Выбор мастера ---
        if data.startswith("mst:"):
            master_id = int(data.split(":")[1])
            step, payload = await get_state(chat_id)
            payload["master_id"] = master_id
            await set_state(chat_id, "done_master", payload)

            await send_message(
                chat_id,
                "Мастер выбран ✅\n\n"
                "Следующий шаг — выбор даты и времени (подключим дальше)."
            )
            return JSONResponse(content={"ok": True})

        await send_message(chat_id, "Не поняла действие. Напишите /start")
        return JSONResponse(content={"ok": True})

    # ====== ОБРАБОТКА ТЕКСТА ======
    message = update.get("message")
    if not message:
        return JSONResponse(content={"ok": True})

    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").lower().strip()

    await upsert_user(chat_id, name=message.get("from", {}).get("first_name"))

    if text in ["/start", "start", "привет", "здравствуйте", "добрый день", "доброе утро", "добрый вечер"]:
        await reset_state(chat_id)

        categories = await get_categories()
        if not categories:
            await send_message(chat_id, "Не удалось загрузить категории 😔")
            return JSONResponse(content={"ok": True})

        buttons = [(c["title"], f"cat:{c['id']}") for c in categories[:30]]
        await send_message(
            chat_id,
            "Здравствуйте 🌸\nВыберите категорию услуг:",
            inline_keyboard(buttons, row=1),
        )
        return JSONResponse(content={"ok": True})

    await send_message(chat_id, "Напишите /start, чтобы начать запись 🌸")
    return JSONResponse(content={"ok": True})
