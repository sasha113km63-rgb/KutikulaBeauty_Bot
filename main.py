import logging
import aiohttp
from datetime import datetime, timedelta

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from config import TELEGRAM_TOKEN
from storage import upsert_user, get_state, set_state, reset_state
from keyboards import inline_keyboard
from yclients_api import (
    get_categories,
    get_services_by_category,
    get_masters_for_service,
    get_available_times,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
app = FastAPI()

MONTHS_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря"
}
WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


async def tg_post(method: str, payload: dict):
    async with aiohttp.ClientSession() as session:
        await session.post(f"{TELEGRAM_API_URL}/{method}", json=payload)


async def send_message(chat_id: int, text: str, reply_markup: dict | None = None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    await tg_post("sendMessage", payload)


async def answer_callback(callback_query_id: str):
    await tg_post("answerCallbackQuery", {"callback_query_id": callback_query_id})


def build_calendar(payload: dict, offset_days: int = 0) -> dict:
    """
    Календарь на 7 дней кнопками.
    ВАЖНО: service_id и master_id передаем в callback кнопки даты,
    поэтому ничего не "собьётся" даже без базы.
    """
    start_date = datetime.now().date() + timedelta(days=offset_days)

    service_id = payload.get("service_id")
    master_id = payload.get("master_id")

    kb = {"inline_keyboard": []}

    for i in range(7):
        d = start_date + timedelta(days=i)
        wd = WEEKDAYS_RU[d.weekday()]
        text = f"{wd} {d.day} {MONTHS_RU[d.month]}"

        # формат: date:YYYY-MM-DD:svc=ID:mst=ID
        cb = f"date:{d.isoformat()}:svc={service_id}:mst={master_id}"
        kb["inline_keyboard"].append([{"text": text, "callback_data": cb}])

    # Навигация
    if offset_days > 0:
        kb["inline_keyboard"].append([
            {"text": "⬅️ назад", "callback_data": f"cal:{offset_days - 7}"},
            {"text": "➡️ вперед", "callback_data": f"cal:{offset_days + 7}"},
        ])
    else:
        kb["inline_keyboard"].append([
            {"text": "➡️ вперед", "callback_data": f"cal:{offset_days + 7}"},
        ])

    return kb


@app.get("/")
async def root():
    return {"status": "ok", "message": "Kutikula bot is running (no DB)"}


@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    logger.info(f"Incoming update: {update}")

    # ---------- КНОПКИ ----------
    if "callback_query" in update:
        cq = update["callback_query"]
        data = cq.get("data", "")
        chat_id = cq["message"]["chat"]["id"]
        await answer_callback(cq["id"])

        # перелистывание календаря
        if data.startswith("cal:"):
            offset = int(data.split(":")[1])
            step, payload = await get_state(chat_id)
            payload["cal_offset"] = offset
            await set_state(chat_id, step, payload)

            # календарь строим из payload (там service_id и master_id)
            await send_message(chat_id, "Выберите дату:", build_calendar(payload, offset))
            return JSONResponse(content={"ok": True})

        # выбор даты
        if data.startswith("date:"):
            # формат: date:YYYY-MM-DD:svc=ID:mst=ID
            parts = data.split(":")
            date_str = parts[1]
            service_id = int(parts[2].split("=")[1])
            master_id = int(parts[3].split("=")[1])

            step, payload = await get_state(chat_id)

            # гарантируем payload (даже если "память" лагнула)
            payload["service_id"] = service_id
            payload["master_id"] = master_id
            payload["date"] = date_str

            await set_state(chat_id, "choose_time", payload)

            times = await get_available_times(service_id=service_id, staff_id=master_id, date_str=date_str)
            if not times:
                await send_message(
                    chat_id,
                    "На эту дату нет свободного времени 😔\nВыберите другую дату:",
                    build_calendar(payload, payload.get("cal_offset", 0)),
                )
                return JSONResponse(content={"ok": True})

            time_buttons = [(t, f"time:{t}") for t in times[:40]]
            await send_message(chat_id, "Выберите время:", inline_keyboard(time_buttons, row=2))
            return JSONResponse(content={"ok": True})

        # выбор времени
        if data.startswith("time:"):
            time_str = data.split("time:")[1]
            step, payload = await get_state(chat_id)

            payload["time"] = time_str
            await set_state(chat_id, "done_time", payload)

            await send_message(
                chat_id,
                f"Отлично ✅\n"
                f"Дата: {payload.get('date')}\n"
                f"Время: {time_str}\n\n"
                f"Следующий шаг — подтвердить запись (и при необходимости запросить имя/телефон)."
            )
            return JSONResponse(content={"ok": True})

        # категория
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

        # услуга
        if data.startswith("svc:"):
            service_id = int(data.split(":")[1])
            step, payload = await get_state(chat_id)
            payload["service_id"] = service_id
            await set_state(chat_id, "choose_master", payload)

            masters = await get_masters_for_service(service_id)
            if not masters:
                await send_message(chat_id, "По этой услуге нет мастеров 😔")
                return JSONResponse(content={"ok": True})

            normalized = []
            for m in masters:
                if isinstance(m, dict):
                    mid = m.get("id")
                    mname = m.get("name")
                    if mid and mname:
                        normalized.append((mname, f"mst:{mid}"))

            if not normalized:
                await send_message(chat_id, "Не смогла прочитать список мастеров 😔")
                return JSONResponse(content={"ok": True})

            await send_message(chat_id, "Выберите мастера:", inline_keyboard(normalized[:30], row=1))
            return JSONResponse(content={"ok": True})

        # мастер
        if data.startswith("mst:"):
            master_id = int(data.split(":")[1])
            step, payload = await get_state(chat_id)

            payload["master_id"] = master_id
            payload["cal_offset"] = 0
            await set_state(chat_id, "choose_date", payload)

            await send_message(chat_id, "Мастер выбран ✅\n\nВыберите дату:", build_calendar(payload, 0))
            return JSONResponse(content={"ok": True})

        await send_message(chat_id, "Не поняла действие. Напишите /start")
        return JSONResponse(content={"ok": True})

    # ---------- ТЕКСТ ----------
    message = update.get("message")
    if not message:
        return JSONResponse(content={"ok": True})

    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip().lower()

    await upsert_user(chat_id, name=(message.get("from", {}) or {}).get("first_name"))

    if text in ["/start", "start", "привет", "здравствуйте", "добрый день", "доброе утро", "добрый вечер"]:
        await reset_state(chat_id)

        categories = await get_categories()
        if not categories:
            await send_message(chat_id, "Не удалось загрузить категории 😔")
            return JSONResponse(content={"ok": True})

        buttons = [(c["title"], f"cat:{c['id']}") for c in categories[:30]]
        await send_message(chat_id, "Здравствуйте 🌸\nВыберите категорию услуг:", inline_keyboard(buttons, row=1))
        return JSONResponse(content={"ok": True})

    await send_message(chat_id, "Напишите /start, чтобы начать запись 🌸")
    return JSONResponse(content={"ok": True})
