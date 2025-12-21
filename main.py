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
    get_free_times,
    create_booking,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
app = FastAPI()

async def tg_request(method: str, payload: dict):
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{TELEGRAM_API_URL}/{method}", json=payload) as r:
            try:
                return await r.json()
            except Exception:
                return {"ok": False}

async def send_message(chat_id: int, text: str, reply_markup: dict | None = None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    await tg_request("sendMessage", payload)

async def answer_callback(callback_query_id: str, text: str | None = None):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
        payload["show_alert"] = False
    await tg_request("answerCallbackQuery", payload)

@app.on_event("startup")
async def on_startup():
    await init_db()
    logger.info("DB initialized")

@app.get("/")
async def root():
    return {"status": "ok"}

def is_start(text: str) -> bool:
    greetings = ["привет", "здравствуйте", "добрый день", "доброе утро", "добрый вечер", "hi", "hello", "/start"]
    t = (text or "").strip().lower()
    return any(w in t for w in greetings)

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    logger.info(f"Incoming update: {update}")

    # 1) callback from inline buttons
    if "callback_query" in update:
        cq = update["callback_query"]
        data = cq.get("data", "")
        chat_id = cq["message"]["chat"]["id"]
        await answer_callback(cq["id"])

        step, payload = await get_state(chat_id)

        # выбор категории
        if data.startswith("cat:"):
            cat_id = int(data.split(":")[1])
            await set_state(chat_id, "choose_service", {"category_id": cat_id})

            services = await get_services_by_category(cat_id)
            if not services:
                await send_message(chat_id, "❌ Не удалось получить услуги. Попробуйте позже.")
                return JSONResponse(content={"ok": True})

            buttons = [(s["title"], f"svc:{s['id']}") for s in services[:30]]
            await send_message(chat_id, "Выберите услугу 💅", inline_keyboard(buttons, row=1))
            return JSONResponse(content={"ok": True})

        # выбор услуги
        if data.startswith("svc:"):
            svc_id = int(data.split(":")[1])
            payload["service_id"] = svc_id
            await set_state(chat_id, "choose_master", payload)

            masters = await get_masters_for_service(svc_id)
            if not masters:
                await send_message(chat_id, "❌ Не удалось получить мастеров по этой услуге.")
                return JSONResponse(content={"ok": True})

            buttons = [(m["name"], f"mst:{m['id']}") for m in masters[:30]]
            await send_message(chat_id, "Выберите мастера 👩‍🎨", inline_keyboard(buttons, row=1))
            return JSONResponse(content={"ok": True})

        # выбор мастера
        if data.startswith("mst:"):
            mst_id = int(data.split(":")[1])
            payload["master_id"] = mst_id
            await set_state(chat_id, "choose_time", payload)

            # Получаем свободное время (упрощённо: ближайший день/период — зависит от твоего yclients_api)
            times = await get_free_times(service_id=payload["service_id"], master_id=mst_id)
            if not times:
                await send_message(chat_id, "😔 Свободных слотов нет. Выберите другую услугу/мастера.")
                return JSONResponse(content={"ok": True})

            # предполагаем, что times = [{"datetime":"2025-12-22 12:00","label":"22.12 12:00"}, ...]
            buttons = [(t.get("label") or t["datetime"], f"time:{t['datetime']}") for t in times[:20]]
            await send_message(chat_id, "Выберите время 🕒", inline_keyboard(buttons, row=2))
            return JSONResponse(content={"ok": True})

        # выбор времени -> финал: create_booking
        if data.startswith("time:"):
            dt = data.split("time:")[1]
            payload["datetime"] = dt
            await set_state(chat_id, "confirm", payload)

            await send_message(
                chat_id,
                f"Подтвердите запись:\n\n• Услуга ID: `{payload['service_id']}`\n• Мастер ID: `{payload['master_id']}`\n• Время: *{dt}*\n\nНажмите кнопку:",
                inline_keyboard([("✅ Подтвердить", "ok:1"), ("❌ Отмена", "cancel:1")], row=2),
            )
            return JSONResponse(content={"ok": True})

        if data.startswith("cancel:"):
            await reset_state(chat_id)
            await send_message(chat_id, "Ок, отменили. Напишите «привет» чтобы начать заново 🌸")
            return JSONResponse(content={"ok": True})

        if data.startswith("ok:"):
            step, payload = await get_state(chat_id)
            if step != "confirm":
                await send_message(chat_id, "Сессия устарела. Напишите «привет» чтобы начать заново 🌸")
                return JSONResponse(content={"ok": True})

            # create booking (зависит от твоей реализации yclients_api)
            result = await create_booking(
                service_id=payload["service_id"],
                master_id=payload["master_id"],
                datetime=payload["datetime"],
                # phone/name можно добавить после запроса контакта
            )

            if not result:
                await send_message(chat_id, "❌ Не удалось создать запись. Попробуйте позже.")
            else:
                await send_message(chat_id, "✅ Запись создана! Мы вас ждём 🌸")
            await reset_state(chat_id)
            return JSONResponse(content={"ok": True})

        await send_message(chat_id, "Не поняла действие. Напишите «привет» 🌸")
        return JSONResponse(content={"ok": True})

    # 2) обычное сообщение
    message = update.get("message")
    if not message:
        return JSONResponse(content={"ok": True})

    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    await upsert_user(chat_id, name=message.get("from", {}).get("first_name"))

    if is_start(text):
        await reset_state(chat_id)
        await send_message(
            chat_id,
            "Здравствуйте!🌸\nЯ — виртуальный администратор *beauty studio KUTIKULA* 💅\n\nВыберите категорию:",
        )
        categories = await get_categories()
        if not categories:
            await send_message(chat_id, "❌ Не удалось загрузить категории услуг.")
            return JSONResponse(content={"ok": True})

        buttons = [(c["title"], f"cat:{c['id']}") for c in categories[:30]]
        await send_message(chat_id, "Категории услуг:", inline_keyboard(buttons, row=1))
        return JSONResponse(content={"ok": True})

    await send_message(chat_id, "Напишите «привет» чтобы начать запись 🌸")
    return JSONResponse(content={"ok": True})
