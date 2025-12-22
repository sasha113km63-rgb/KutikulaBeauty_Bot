import os
import json
import re
import logging
import aiohttp
from datetime import datetime, date, timedelta
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from config import TELEGRAM_TOKEN, YCLIENTS_COMPANY_ID
from yclients_api import (
    get_categories,
    get_services_by_category,
    get_masters_for_service,
    create_booking,
    get_headers,          # берем готовые заголовки авторизации из yclients_api.py
    BASE_URL,             # берем базовый URL из yclients_api.py
)

# ------------------- НАСТРОЙКИ -------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

app = FastAPI()

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
MEMORY_FILE = "dialog_memory.json"

# чтобы не слать одно и то же 3 раза при повторных callback
PROCESSED_CALLBACKS_TTL_SEC = 120
processed_callbacks = {}  # callback_id -> unix_ts

# ------------------- ПАМЯТЬ (без базы) -------------------
def _load_memory():
    try:
        if not os.path.exists(MEMORY_FILE):
            return {}
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return {}
    except Exception:
        return {}

def _save_memory(mem: dict):
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(mem, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Не смог сохранить память: {e}")

def get_state(chat_id: int) -> dict:
    mem = _load_memory()
    return mem.get(str(chat_id), {"step": "idle", "data": {}})

def set_state(chat_id: int, step: str, data: dict):
    mem = _load_memory()
    mem[str(chat_id)] = {"step": step, "data": data}
    _save_memory(mem)

def reset_state(chat_id: int):
    set_state(chat_id, "idle", {})

# ------------------- TELEGRAM HELPERS -------------------
async def tg_post(method: str, payload: dict):
    url = f"{TELEGRAM_API}/{method}"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            try:
                return await resp.json()
            except Exception:
                text = await resp.text()
                return {"ok": False, "raw": text}

async def send_message(chat_id: int, text: str, reply_markup: dict | None = None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await tg_post("sendMessage", payload)

async def edit_message(chat_id: int, message_id: int, text: str, reply_markup: dict | None = None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await tg_post("editMessageText", payload)

async def answer_callback(callback_id: str):
    # убирает "часики" на кнопке, иначе телега может дергать повторно
    return await tg_post("answerCallbackQuery", {"callback_query_id": callback_id})

def inline_keyboard(button_rows):
    return {"inline_keyboard": button_rows}

# ------------------- КАЛЕНДАРЬ -------------------
RU_WEEK = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
RU_MONTH = [
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря"
]

def fmt_day(d: date) -> str:
    wd = RU_WEEK[d.weekday()]
    return f"{wd} {d.day} {RU_MONTH[d.month]}"

def build_calendar(service_id: int, master_id: int, offset_days: int = 0):
    start = date.today() + timedelta(days=offset_days)
    days = [start + timedelta(days=i) for i in range(7)]

    rows = []
    for d in days:
        cb = f"date:{d.isoformat()}:svc={service_id}:mst={master_id}:off={offset_days}"
        rows.append([{"text": fmt_day(d), "callback_data": cb}])

    nav = [
        {"text": "⬅️ назад", "callback_data": f"cal:{service_id}:{master_id}:{max(offset_days-7,0)}"},
        {"text": "➡️ вперед", "callback_data": f"cal:{service_id}:{master_id}:{offset_days+7}"},
    ]
    rows.append(nav)
    return inline_keyboard(rows)

# ------------------- YCLIENTS: свободное время (исправлено) -------------------
async def get_free_times_for_date(staff_id: int, service_id: int, day_iso: str):
    """
    Правильный паттерн эндпойнта для book_times:
    /book_times/{company_id}/{staff_id}/{date}
    + в query пробуем передать service_ids[] (на случай, если филиал требует фильтрацию по услуге).
    """
    headers = await get_headers()

    # 1) основной вариант
    url = f"{BASE_URL}/book_times/{YCLIENTS_COMPANY_ID}/{staff_id}/{day_iso}"
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
                logger.error(f"Ошибка запроса времени: {e}")
                continue

            # иногда YCLIENTS может вернуть 404/ошибку формата — пробуем следующий вариант params
            if isinstance(data, dict) and data.get("success") is False:
                # если это не "успешно", пробуем следующий params
                logger.error(f"get_free_times_for_date неуспешно: {data}")
                continue

            # если пришел не dict/неожиданно — тоже пробуем дальше
            if not isinstance(data, dict) or "data" not in data:
                logger.error(f"Неожиданный ответ book_times: {data}")
                continue

            # формат 1: data = ["10:00", "10:30", ...]
            if isinstance(data["data"], list) and data["data"] and isinstance(data["data"][0], str):
                return [f"{day_iso} {t}" for t in data["data"]]

            # формат 2: data = [{"time":"10:00"}, ...] или другое
            if isinstance(data["data"], list):
                times = []
                for item in data["data"]:
                    if isinstance(item, dict):
                        t = item.get("time") or item.get("datetime") or item.get("start")
                        if t:
                            times.append(str(t))
                # если это уже "YYYY-MM-DD HH:MM" — вернем как есть
                if times and re.match(r"^\d{4}-\d{2}-\d{2}", times[0]):
                    return times
                # если это "HH:MM"
                if times:
                    return [f"{day_iso} {t}" for t in times]
                return []

            return []

    return []

# ------------------- UI: меню/шаги -------------------
def main_menu():
    return inline_keyboard([
        [{"text": "✅ Записаться на процедуру", "callback_data": "menu:book"}],
        [{"text": "📋 Посмотреть услуги", "callback_data": "menu:services"}],
    ])

async def show_welcome(chat_id: int):
    text = (
        "Здравствуйте 🌸\n"
        "Я — виртуальный администратор студии <b>KUTIKULA</b>.\n\n"
        "Чем могу помочь?"
    )
    await send_message(chat_id, text, main_menu())
    reset_state(chat_id)

# ------------------- ОБРАБОТЧИКИ -------------------
async def handle_menu(chat_id: int, action: str):
    if action == "book":
        cats = await get_categories()
        if not cats:
            await send_message(chat_id, "❌ Не получилось получить категории из YCLIENTS.")
            return

        rows = []
        for c in cats:
            rows.append([{"text": c["title"], "callback_data": f"cat:{c['id']}"}])

        await send_message(chat_id, "Выберите категорию услуг:", inline_keyboard(rows))
        set_state(chat_id, "choosing_category", {})
        return

    if action == "services":
        cats = await get_categories()
        if not cats:
            await send_message(chat_id, "❌ Не получилось получить категории из YCLIENTS.")
            return

        msg = "Категории:\n\n" + "\n".join([f"• {c['title']}" for c in cats])
        await send_message(chat_id, msg)
        return

    await send_message(chat_id, "Не поняла команду. Напишите /start")

async def handle_category(chat_id: int, category_id: int):
    services = await get_services_by_category(category_id)
    if not services:
        await send_message(chat_id, "❌ В этой категории нет услуг или не удалось загрузить.")
        return

    rows = []
    for s in services[:80]:  # защита от слишком длинных списков
        rows.append([{"text": s["title"], "callback_data": f"svc:{s['id']}"}])

    await send_message(chat_id, "Выберите услугу:", inline_keyboard(rows))
    set_state(chat_id, "choosing_service", {"category_id": category_id})

async def handle_service(chat_id: int, service_id: int):
    masters = await get_masters_for_service(service_id)
    if not masters:
        await send_message(chat_id, "По этой услуге нет мастеров 😔\nВыберите другую услугу.")
        return

    rows = []
    for m in masters[:80]:
        rows.append([{"text": m["name"], "callback_data": f"mst:{m['id']}:svc={service_id}"}])

    await send_message(chat_id, "Выберите мастера:", inline_keyboard(rows))
    set_state(chat_id, "choosing_master", {"service_id": service_id})

async def handle_master(chat_id: int, master_id: int, service_id: int):
    kb = build_calendar(service_id=service_id, master_id=master_id, offset_days=0)
    await send_message(chat_id, "Выберите дату:", kb)
    set_state(chat_id, "choosing_date", {"service_id": service_id, "master_id": master_id, "offset": 0})

async def handle_calendar_nav(chat_id: int, service_id: int, master_id: int, offset_days: int, message_id: int):
    kb = build_calendar(service_id=service_id, master_id=master_id, offset_days=offset_days)
    await edit_message(chat_id, message_id, "Выберите дату:", kb)
    set_state(chat_id, "choosing_date", {"service_id": service_id, "master_id": master_id, "offset": offset_days})

async def handle_date(chat_id: int, day_iso: str, service_id: int, master_id: int):
    times = await get_free_times_for_date(master_id, service_id, day_iso)

    if not times:
        kb = build_calendar(service_id=service_id, master_id=master_id, offset_days=0)
        await send_message(chat_id, "На эту дату нет свободного времени 😔\nВыберите другую дату:", kb)
        set_state(chat_id, "choosing_date", {"service_id": service_id, "master_id": master_id, "offset": 0})
        return

    # показываем времена кнопками
    rows = []
    for t in times[:60]:
        # t = "YYYY-MM-DD HH:MM"
        cb = f"time:{t}:svc={service_id}:mst={master_id}"
        # для текста берем только HH:MM
        hhmm = t.split(" ")[1][:5] if " " in t else t
        rows.append([{"text": hhmm, "callback_data": cb}])

    await send_message(chat_id, "Выберите время:", inline_keyboard(rows))
    set_state(chat_id, "choosing_time", {"service_id": service_id, "master_id": master_id, "date": day_iso})

async def handle_time(chat_id: int, datetime_str: str, service_id: int, master_id: int):
    # просим имя
    set_state(chat_id, "await_name", {"service_id": service_id, "master_id": master_id, "datetime": datetime_str})
    await send_message(chat_id, "Как вас зовут? (только имя)")

def normalize_phone(text: str) -> str | None:
    digits = re.sub(r"\D+", "", text or "")
    if len(digits) < 10:
        return None
    # приводим к формату +7...
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if not digits.startswith("7") and len(digits) == 10:
        digits = "7" + digits
    return "+" + digits

# ------------------- WEBHOOK -------------------
@app.get("/")
async def root():
    return {"status": "ok", "message": "Kutikula bot is running"}

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    logger.info(f"Incoming update: {update}")

    # 1) callback (нажатия кнопок)
    if "callback_query" in update:
        cq = update["callback_query"]
        cq_id = cq.get("id")
        data = cq.get("data", "")
        msg = cq.get("message", {})
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        message_id = msg.get("message_id")

        # анти-дубликаты
        now_ts = int(datetime.utcnow().timestamp())
        # чистим старые
        for k, v in list(processed_callbacks.items()):
            if now_ts - v > PROCESSED_CALLBACKS_TTL_SEC:
                processed_callbacks.pop(k, None)
        if cq_id in processed_callbacks:
            await answer_callback(cq_id)
            return JSONResponse(content={"ok": True})
        processed_callbacks[cq_id] = now_ts

        await answer_callback(cq_id)

        try:
            # menu:
            if data.startswith("menu:"):
                action = data.split(":")[1]
                await handle_menu(chat_id, action)
                return JSONResponse(content={"ok": True})

            # cat:
            if data.startswith("cat:"):
                category_id = int(data.split(":")[1])
                await handle_category(chat_id, category_id)
                return JSONResponse(content={"ok": True})

            # svc:
            if data.startswith("svc:"):
                service_id = int(data.split(":")[1])
                await handle_service(chat_id, service_id)
                return JSONResponse(content={"ok": True})

            # mst:
            if data.startswith("mst:"):
                # mst:{master_id}:svc={service_id}
                parts = data.split(":")
                master_id = int(parts[1])
                service_id = int(parts[2].split("=")[1])
                await handle_master(chat_id, master_id, service_id)
                return JSONResponse(content={"ok": True})

            # cal:
            if data.startswith("cal:"):
                # cal:{service_id}:{master_id}:{offset}
                _, svc, mst, off = data.split(":")
                await handle_calendar_nav(
                    chat_id=int(chat_id),
                    service_id=int(svc),
                    master_id=int(mst),
                    offset_days=int(off),
                    message_id=int(message_id),
                )
                return JSONResponse(content={"ok": True})

            # date:
            if data.startswith("date:"):
                # date:YYYY-MM-DD:svc=...:mst=...:off=...
                parts = data.split(":")
                day_iso = parts[1]
                service_id = int(parts[2].split("=")[1])
                master_id = int(parts[3].split("=")[1])
                await handle_date(chat_id, day_iso, service_id, master_id)
                return JSONResponse(content={"ok": True})

            # time:
            if data.startswith("time:"):
                # time:YYYY-MM-DD HH:MM:svc=...:mst=...
                parts = data.split(":")
                dt = parts[1]  # "YYYY-MM-DD HH"
                mm = parts[2]  # "MM"
                datetime_str = f"{dt}:{mm}"  # "YYYY-MM-DD HH:MM"
                service_id = int(parts[3].split("=")[1])
                master_id = int(parts[4].split("=")[1])
                await handle_time(chat_id, datetime_str, service_id, master_id)
                return JSONResponse(content={"ok": True})

            await send_message(chat_id, "Что-то сбилось. Напишите /start и попробуйте снова.")
            return JSONResponse(content={"ok": True})

        except Exception as e:
            logger.exception(e)
            await send_message(chat_id, "Что-то сбилось. Напишите /start и попробуйте снова.")
            return JSONResponse(content={"ok": True})

    # 2) обычные сообщения (текст)
    message = update.get("message")
    if not message:
        return JSONResponse(content={"ok": True})

    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()

    # команды старта
    if text in ("/start", "start", "привет", "Привет", "Здравствуйте", "здравствуйте"):
        await show_welcome(chat_id)
        return JSONResponse(content={"ok": True})

    st = get_state(chat_id)
    step = st.get("step", "idle")
    data = st.get("data", {})

    try:
        if step == "await_name":
            name = text
            if len(name) < 2:
                await send_message(chat_id, "Напишите имя чуть понятнее (минимум 2 буквы).")
                return JSONResponse(content={"ok": True})
            data["name"] = name
            set_state(chat_id, "await_phone", data)
            await send_message(chat_id, "Теперь напишите номер телефона (можно в любом формате).")
            return JSONResponse(content={"ok": True})

        if step == "await_phone":
            phone = normalize_phone(text)
            if not phone:
                await send_message(chat_id, "Не вижу корректный номер. Пример: +7 917 123-45-67")
                return JSONResponse(content={"ok": True})

            name = data["name"]
            service_id = int(data["service_id"])
            master_id = int(data["master_id"])
            dt_str = data["datetime"]

            # создаем запись в YCLIENTS
            booking = await create_booking(
                name=name,
                last_name="",
                phone=phone,
                service_id=service_id,
                master_id=master_id,
                time=dt_str,
            )

            if booking:
                await send_message(chat_id, f"✅ Готово! Вы записаны на <b>{dt_str}</b>.\nЕсли нужно перенести — напишите мне.")
                reset_state(chat_id)
            else:
                await send_message(chat_id, "❌ Не получилось создать запись. Попробуйте выбрать другое время или напишите /start.")
                reset_state(chat_id)

            return JSONResponse(content={"ok": True})

        # если не в процессе — мягко возвращаем в меню
        await send_message(chat_id, "Напишите /start, чтобы начать запись.")
        return JSONResponse(content={"ok": True})

    except Exception as e:
        logger.exception(e)
        await send_message(chat_id, "Что-то сбилось. Напишите /start и попробуйте снова.")
        reset_state(chat_id)
        return JSONResponse(content={"ok": True})
    await send_message(chat_id, "Напишите /start, чтобы начать запись 🌸")
    return JSONResponse(content={"ok": True})
