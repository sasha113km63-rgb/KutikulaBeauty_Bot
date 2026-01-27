import os
import json
import re
import logging
import aiohttp
import html
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from config import TELEGRAM_TOKEN, YCLIENTS_COMPANY_ID
from yclients_api import (
    # оставлено для совместимости (старый сценарий записи)
    get_categories,
    get_services_by_category,
    get_masters_for_service,
    create_booking,
    # для запросов к YCLIENTS
    get_headers,
    BASE_URL,
    get_record_by_id,
)

# ------------------- УТИЛИТЫ -------------------
def safe_str(x) -> str:
    return "" if x is None else str(x)

def escape_html(s: str) -> str:
    return html.escape(s or "")

def try_parse_dt(s: str):
    """Пытаемся распарсить дату/время из разных форматов (в т.ч. '2026-01-27 15:30:00')."""
    if not s:
        return None
    s = str(s).strip()
    # ISO (иногда с Z)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        pass
    # Частые форматы
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None

def normalize_phone(text: str) -> str | None:
    digits = re.sub(r"\D+", "", text or "")
    if len(digits) < 10:
        return None
    # приводим к +7...
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if not digits.startswith("7") and len(digits) == 10:
        digits = "7" + digits
    if len(digits) != 11:
        return None
    return "+" + digits

def md_sanitize(s: str) -> str:
    """Мини-санитайзер под Telegram Markdown (legacy), чтобы динамические поля не ломали разметку."""
    if not s:
        return ""
    # экранируем самые частые "ломающие" символы
    for ch in ["*", "_", "`", "[", "]"]:
        s = s.replace(ch, f"\\{ch}")
    return s

# ------------------- ЛОГИ/APP -------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

app = FastAPI()

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ------------------- НАСТРОЙКИ (ENV) -------------------
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
ONLINE_BOOKING_URL = os.getenv("ONLINE_BOOKING_URL", "https://n561655.yclients.com/")
BOOKING_ENABLED = os.getenv("BOOKING_ENABLED", "false").lower() == "true"

# вебхук YCLIENTS (секрет)
YCLIENTS_WEBHOOK_SECRET = os.getenv("YCLIENTS_WEBHOOK_SECRET", "")

# память (привязка телефона и т.п.)
MEMORY_FILE = "dialog_memory.json"
# чтобы не дублить отбивки
SENT_FILE = "sent_events.json"

# ------------------- ХРАНИЛКИ -------------------
def _load_json(path: str) -> dict:
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def _save_json(path: str, data: dict):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Не смог сохранить {path}: {e}")

def get_state(chat_id: int) -> dict:
    mem = _load_json(MEMORY_FILE)
    return mem.get(str(chat_id), {"step": "idle", "data": {}})

def set_state(chat_id: int, step: str, data: dict):
    mem = _load_json(MEMORY_FILE)
    mem[str(chat_id)] = {"step": step, "data": data}
    _save_json(MEMORY_FILE, mem)

def reset_state(chat_id: int):
    set_state(chat_id, "idle", {})

def phone_to_chat_map() -> dict[str, int]:
    """phone(+7...) -> chat_id"""
    mem = _load_json(MEMORY_FILE)
    out = {}
    for chat_id_str, st in mem.items():
        data = (st or {}).get("data", {}) or {}
        ph = data.get("phone")
        if ph:
            out[str(ph)] = int(chat_id_str)
    return out

def was_sent(record_id: str, kind: str) -> bool:
    sent = _load_json(SENT_FILE)
    return bool(sent.get(record_id, {}).get(kind))

def mark_sent(record_id: str, kind: str, extra: dict | None = None):
    sent = _load_json(SENT_FILE)
    sent.setdefault(record_id, {})
    sent[record_id][kind] = extra or True
    _save_json(SENT_FILE, sent)

# ------------------- TELEGRAM HELPERS -------------------
async def tg_post(method: str, payload: dict):
    url = f"{TELEGRAM_API}/{method}"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            try:
                return await resp.json()
            except Exception:
                return {"ok": False, "raw": await resp.text()}

async def send_message(chat_id: int, text: str, reply_markup: dict | None = None, parse_mode: str = "Markdown"):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode, "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await tg_post("sendMessage", payload)

async def answer_callback(callback_id: str):
    return await tg_post("answerCallbackQuery", {"callback_query_id": callback_id})

def inline_keyboard(rows):
    return {"inline_keyboard": rows}

def is_admin_chat(chat_id: int) -> bool:
    return ADMIN_CHAT_ID != 0 and chat_id == ADMIN_CHAT_ID

async def notify_admin(text_html: str):
    """Админ-логи отправляем в HTML."""
    if ADMIN_CHAT_ID == 0:
        return
    await tg_post("sendMessage", {
        "chat_id": ADMIN_CHAT_ID,
        "text": text_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })

async def send_client(chat_id: int, text_md: str, reply_markup: dict | None = None, meta: str | None = None):
    """Клиенту отправляем в Markdown (ваши *жирные* и _курсив_ работают)."""
    res = await send_message(chat_id, text_md, reply_markup=reply_markup, parse_mode="Markdown")
    if not is_admin_chat(chat_id):
        ok = bool(res.get("ok"))
        status = "ОТПРАВЛЕНО" if ok else f"ОШИБКА: {escape_html(safe_str(res))}"
        meta_txt = f"<b>{escape_html(meta)}</b><br/>" if meta else ""
        await notify_admin(
            f"""{meta_txt}<b>➡️ Исходящее клиенту</b><br/>
chat_id: <code>{chat_id}</code><br/>
Статус: <b>{status}</b><br/><br/>
{escape_html(text_md)[:3500]}"""
        )
    return res

# ------------------- UI -------------------
def main_menu():
    return inline_keyboard([
        [{"text": "📅 Онлайн-запись", "url": ONLINE_BOOKING_URL}],
        [{"text": "💬 Написать администратору", "callback_data": "menu:to_admin"}],
        [{"text": "📱 Привязать номер", "callback_data": "menu:link_phone"}],
    ])

def contact_keyboard():
    return {
        "keyboard": [[{"text": "📱 Отправить номер", "request_contact": True}]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }

WELCOME_TEXT = (
    "Здравствуйте 🌸\n"
    "Я — виртуальный администратор студии KUTIKULA.\n\n"
    "Я могу присылать вам напоминание о Вашей записи. За три дня, за один день и за пару часов до записи.\n\n"
    "Изменить запись вы сможете с помощью онлайн записи перейдя по ссылке:\n"
    f"{ONLINE_BOOKING_URL}"
)

async def show_welcome(chat_id: int):
    await send_client(chat_id, WELCOME_TEXT, reply_markup=main_menu(), meta="WELCOME")
    reset_state(chat_id)

# ------------------- ШАБЛОН ОТБИВКИ -------------------
ADDRESS_BLOCK = (
    "Aдрес cтудии\n"
    "ул. Фасаднaя, д. 21\n\n"
    "Вхoд сo стороны улицы Фacaдная\n"
    "Яндeкc.Карты\n"
    "https://kutikula116.clients.site"
)

def tpl_booking_created(service: str, master: str, price: str, dt_str: str) -> str:
    return (
        "👋 Вы записaны в\n"
        "Studio KUTIKULA\n\n"
        f"▫️{service}\n"
        f"{master}\n"
        f"{price}\n"
        f"{dt_str}\n\n"
        f"{ADDRESS_BLOCK}\n\n"
        "Ждём Bаc!"
    )

# ------------------- /chatid -------------------
async def send_chatid(chat_id: int):
    await send_message(chat_id, f"chat_id = {chat_id}", parse_mode="Markdown")

# ------------------- YCLIENTS WEBHOOK -------------------
def extract_from_yclients_webhook(payload: dict) -> dict:
    """
    YCLIENTS присылает часто такую форму:
    {"company_id":..., "resource":"record", "resource_id":..., "status":"create|update|delete", "data":{...}}
    Внутри payload["data"] обычно НЕТ телефона клиента, поэтому при отсутствии телефона мы будем
    дополнительно запрашивать запись по record_id через API.
    """
    d = payload.get("data") if isinstance(payload.get("data"), dict) else payload

    # статус
    status = safe_str(payload.get("status") or d.get("status") or "").lower().strip()

    # record_id может быть в resource_id, либо в data.id
    record_id = payload.get("resource_id") or d.get("id") or d.get("record_id") or d.get("appointment_id") or d.get("event_id")
    record_id = safe_str(record_id)

    # company_id может отличаться от env — берём из payload если есть
    company_id = payload.get("company_id") or d.get("company_id") or YCLIENTS_COMPANY_ID
    try:
        company_id = int(company_id)
    except Exception:
        company_id = int(YCLIENTS_COMPANY_ID)

    # телефон (часто отсутствует)
    phone_raw = None
    if isinstance(d.get("client"), dict):
        phone_raw = d["client"].get("phone") or d["client"].get("phone_number")
    phone_raw = phone_raw or d.get("phone") or d.get("client_phone")
    phone = normalize_phone(safe_str(phone_raw)) or safe_str(phone_raw)

    # дата/время в webhook чаще приходит как "date"
    start_str = d.get("start_at") or d.get("datetime") or d.get("date_time") or d.get("seance_date") or d.get("date")
    start_dt = try_parse_dt(start_str) if start_str else None

    return {
        "status": status,
        "record_id": record_id,
        "company_id": company_id,
        "phone": phone,
        "start_dt": start_dt,
        "raw": payload,
    }

def extract_from_record_detail(rec: dict) -> dict:
    """Вынимаем из полной записи телефон/услугу/мастера/стоимость/дату."""
    phone_raw = None
    if isinstance(rec.get("client"), dict):
        phone_raw = rec["client"].get("phone") or rec["client"].get("phone_number")
    phone_raw = phone_raw or rec.get("client_phone") or rec.get("phone")
    phone = normalize_phone(safe_str(phone_raw)) or safe_str(phone_raw)

    # datetime
    start_str = rec.get("datetime") or rec.get("date") or rec.get("start_at")
    start_dt = try_parse_dt(start_str) if start_str else None

    # service/title
    service = ""
    price = ""

    if isinstance(rec.get("services"), list) and rec["services"]:
        s0 = rec["services"][0]
        if isinstance(s0, dict):
            service = s0.get("title") or s0.get("name") or ""
            if s0.get("price") is not None:
                price = safe_str(s0.get("price"))

    if not service and isinstance(rec.get("service"), dict):
        service = rec["service"].get("title") or rec["service"].get("name") or service
        if not price and rec["service"].get("price") is not None:
            price = safe_str(rec["service"].get("price"))

    if not price:
        price = safe_str(rec.get("price") or rec.get("cost") or rec.get("amount") or "")

    master = ""
    if isinstance(rec.get("staff"), dict):
        master = rec["staff"].get("name") or master
    if isinstance(rec.get("master"), dict):
        master = rec["master"].get("name") or master

    return {
        "phone": phone,
        "start_dt": start_dt,
        "service": safe_str(service),
        "master": safe_str(master),
        "price": safe_str(price),
    }

@app.post("/yclients-webhook")
async def yclients_webhook(request: Request):
    # секрет можно передавать query или заголовком (на всякий случай)
    secret_q = request.query_params.get("secret", "")
    secret_h = request.headers.get("X-Webhook-Secret", "")
    incoming_secret = secret_q or secret_h

    if YCLIENTS_WEBHOOK_SECRET and incoming_secret != YCLIENTS_WEBHOOK_SECRET:
        return JSONResponse(status_code=403, content={"ok": False, "error": "forbidden"})

    payload = await request.json()
    logger.info(f"YCLIENTS webhook: {payload}")

    f = extract_from_yclients_webhook(payload)

    # нас интересует отбивка в момент создания записи
    create_statuses = {"create", "created", "new"}
    if f["status"] and (f["status"] not in create_statuses):
        # игнорим update/delete чтобы не слать лишнее
        return {"ok": True}

    record_id = f["record_id"]
    company_id = f["company_id"]

    if record_id and was_sent(record_id, "created"):
        return {"ok": True}

    # если нет телефона в webhook — достаем полную запись по id
    details = {"phone": f["phone"], "start_dt": f["start_dt"], "service": "", "master": "", "price": ""}
    if not details["phone"]:
        rec = await get_record_by_id(company_id, record_id)
        if rec:
            det = extract_from_record_detail(rec)
            details.update(det)

    if not details["phone"]:
        await notify_admin(
            f"<b>YCLIENTS webhook</b><br/>"
            f"record_id: <code>{escape_html(record_id)}</code><br/>"
            f"Не нашла телефон (ни в webhook, ни в деталях записи).<br/>"
            f"<pre>{escape_html(json.dumps(payload, ensure_ascii=False)[:1500])}</pre>"
        )
        return {"ok": True}

    phone_map = phone_to_chat_map()
    chat_id = phone_map.get(str(details["phone"]))

    if not chat_id:
        await notify_admin(
            f"<b>Новая запись (YCLIENTS)</b><br/>"
            f"record_id: <code>{escape_html(record_id)}</code><br/>"
            f"Телефон: <code>{escape_html(details['phone'])}</code><br/>"
            f"Клиент не привязан к боту (не отправлял номер)."
        )
        return {"ok": True}

    # формируем текст
    if details["start_dt"]:
        dt_line = f"Дата и время визита: {details['start_dt'].strftime('%d.%m.%Y %H:%M')}"
    else:
        dt_line = "Дата и время визита: уточните у администратора"

    # динамические поля санитайзим, чтобы не ломали Markdown
    service_txt = md_sanitize(details["service"] or "УСЛУГА")
    master_name = md_sanitize(details["master"]) if details["master"] else ""
    master_txt = master_name if master_name else "*к какому Mастеру*"

    price_val = md_sanitize(details["price"]) if details["price"] else ""
    price_txt = f"Предварительная cтoимoсть: {price_val}" if price_val else "Предварительная cтoимoсть: —"

    msg = tpl_booking_created(
        service=service_txt,
        master=master_txt,
        price=price_txt,
        dt_str=dt_line,
    )
    await send_client(chat_id, msg, meta="BOOKING_CREATED_WEBHOOK")

    if record_id:
        mark_sent(record_id, "created", {"src": "webhook", "ts": datetime.utcnow().isoformat(), "phone": details["phone"], "chat_id": chat_id})

    await notify_admin(
        f"<b>✅ Отбивка отправлена</b><br/>"
        f"chat_id: <code>{chat_id}</code><br/>"
        f"тел: <code>{escape_html(details['phone'])}</code><br/>"
        f"record_id: <code>{escape_html(record_id)}</code>"
    )
    return {"ok": True}

# ------------------- TELEGRAM WEBHOOK -------------------
@app.get("/")
async def root():
    return {"status": "ok"}

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    logger.info(f"Incoming update: {update}")

    # callback-кнопки
    if "callback_query" in update:
        cq = update["callback_query"]
        cq_id = cq.get("id")
        data = cq.get("data", "")
        msg = cq.get("message", {})
        chat = msg.get("chat", {})
        chat_id = chat.get("id")

        await tg_post("answerCallbackQuery", {"callback_query_id": cq_id})

        if data.startswith("menu:"):
            action = data.split(":", 1)[1]
            if action == "to_admin":
                st = get_state(chat_id)
                set_state(chat_id, "chat_to_admin", st.get("data", {}))
                await send_client(chat_id, "Напишите сообщение — я перешлю администратору.", meta="TO_ADMIN")
                return JSONResponse(content={"ok": True})

            if action == "link_phone":
                st = get_state(chat_id)
                set_state(chat_id, "await_contact", st.get("data", {}))
                await send_client(
                    chat_id,
                    "Нажмите кнопку ниже, чтобы отправить номер телефона (нужно для напоминаний о записи).",
                    reply_markup={
                        "keyboard": [[{"text": "📱 Отправить номер", "request_contact": True}]],
                        "resize_keyboard": True,
                        "one_time_keyboard": True,
                    },
                    meta="LINK_PHONE",
                )
                return JSONResponse(content={"ok": True})

        # Старый сценарий записи — только если включили BOOKING_ENABLED=true
        if (not BOOKING_ENABLED) and data.startswith(("cat:", "svc:", "mst:", "cal:", "date:", "time:", "menu:book", "menu:services")):
            await send_client(chat_id, f"Запись через бота отключена. Используйте онлайн-запись: {ONLINE_BOOKING_URL}", reply_markup=main_menu(), meta="BOOKING_DISABLED")
            return JSONResponse(content={"ok": True})

        return JSONResponse(content={"ok": True})

    # обычные сообщения
    message = update.get("message")
    if not message:
        return JSONResponse(content={"ok": True})

    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()

    # /chatid (в группе придёт как /chatid@KutikulaBeauty_Bot — поэтому startswith)
    if text.startswith("/chatid"):
        await send_chatid(chat_id)
        return JSONResponse(content={"ok": True})

    # контакт (кнопка «Отправить номер»)
    contact = message.get("contact")
    if contact:
        phone_raw = contact.get("phone_number", "")
        phone = normalize_phone(phone_raw) or phone_raw

        st = get_state(chat_id)
        data_mem = st.get("data", {})
        data_mem["phone"] = phone
        set_state(chat_id, "idle", data_mem)

        await notify_admin(
            f"<b>📱 Клиент отправил контакт</b><br/>"
            f"chat_id: <code>{chat_id}</code><br/>"
            f"тел: <code>{escape_html(phone)}</code>"
        )
        await send_client(chat_id, "Спасибо! Номер сохранён.", reply_markup=main_menu(), meta="CONTACT_SAVED")
        return JSONResponse(content={"ok": True})

    # приветствия + /start
    if text.lower() in ("/start", "start", "привет", "здравствуйте", "добрый день", "добрый вечер"):
        await show_welcome(chat_id)
        return JSONResponse(content={"ok": True})

    # если прислали номер текстом
    ph = normalize_phone(text)
    if ph:
        st = get_state(chat_id)
        data_mem = st.get("data", {})
        data_mem["phone"] = ph
        set_state(chat_id, "idle", data_mem)
        await notify_admin(
            f"<b>📱 Клиент прислал номер текстом</b><br/>chat_id: <code>{chat_id}</code><br/>тел: <code>{escape_html(ph)}</code>"
        )
        await send_client(chat_id, "Спасибо! Номер сохранён.", reply_markup=main_menu(), meta="PHONE_SAVED_TEXT")
        return JSONResponse(content={"ok": True})

    # режим передачи админу
    st = get_state(chat_id)
    step = st.get("step", "idle")

    if text:
        await notify_admin(
            f"<b>📩 Входящее от клиента</b><br/>"
            f"chat_id: <code>{chat_id}</code><br/>"
            f"Текст:<br/>{escape_html(text)[:3500]}"
        )

    if step == "chat_to_admin":
        await send_client(chat_id, "Сообщение передано администратору. Ответим вам в этом чате.", reply_markup=main_menu(), meta="MSG_TO_ADMIN_OK")
        set_state(chat_id, "idle", st.get("data", {}))
        return JSONResponse(content={"ok": True})

    # дефолтный ответ
    await send_client(
        chat_id,
        "Принято.\n\nЧтобы записаться — нажмите «Онлайн-запись».\nЕсли нужен администратор — нажмите «Написать администратору».",
        reply_markup=main_menu(),
        meta="DEFAULT_REPLY",
    )
    return JSONResponse(content={"ok": True})
