import os
import json
import re
import logging
import aiohttp
import html
import asyncio
from datetime import datetime, timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from config import TELEGRAM_TOKEN, YCLIENTS_COMPANY_ID

# Мягкий импорт: деплой не должен падать из‑за несовпадения функций
try:
    from yclients_api import (
        get_categories,
        get_services_by_category,
        get_masters_for_service,
        create_booking,
        get_headers,
        BASE_URL,
        get_record_by_id,
    )
except Exception:
    from yclients_api import (
        get_categories,
        get_services_by_category,
        get_masters_for_service,
        create_booking,
        get_headers,
        BASE_URL,
    )
    get_record_by_id = None  # type: ignore

# ------------------- УТИЛИТЫ -------------------
def safe_str(x) -> str:
    return "" if x is None else str(x)

def escape_html(s: str) -> str:
    return html.escape(s or "")

def try_parse_dt(s: str):
    if not s:
        return None
    s = str(s).strip()
    try:
        # ISO, иногда с Z
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        pass
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
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if not digits.startswith("7") and len(digits) == 10:
        digits = "7" + digits
    if len(digits) != 11:
        return None
    return "+" + digits

def md_sanitize(s: str) -> str:
    if not s:
        return ""
    for ch in ["*", "_", "`", "[", "]"]:
        s = s.replace(ch, f"\\{ch}")
    return s

def first_non_empty(*vals: Any) -> str:
    for v in vals:
        if v is None:
            continue
        if isinstance(v, str) and v.strip():
            return v.strip()
        if not isinstance(v, str):
            sv = safe_str(v).strip()
            if sv:
                return sv
    return ""

# ------------------- ЛОГИ/APP -------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

app = FastAPI()
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ------------------- ENV -------------------
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
ONLINE_BOOKING_URL = os.getenv("ONLINE_BOOKING_URL", "https://n561655.yclients.com/")
YCLIENTS_WEBHOOK_SECRET = os.getenv("YCLIENTS_WEBHOOK_SECRET", "")

# Время студии: по умолчанию Самара/МСК+1 = UTC+4
STUDIO_TZ_OFFSET_HOURS = int(os.getenv("STUDIO_TZ_OFFSET_HOURS", "4"))

MEMORY_FILE = "dialog_memory.json"
SENT_FILE = "sent_events.json"
REMINDERS_FILE = "reminders.json"

# ------------------- JSON STORAGE -------------------
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

def _now_studio() -> datetime:
    return datetime.utcnow() + timedelta(hours=STUDIO_TZ_OFFSET_HOURS)

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

def inline_keyboard(rows):
    return {"inline_keyboard": rows}

async def notify_admin(text_html: str):
    if ADMIN_CHAT_ID == 0:
        return
    await tg_post("sendMessage", {
        "chat_id": ADMIN_CHAT_ID,
        "text": text_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })

async def send_client(chat_id: int, text_md: str, reply_markup: dict | None = None, meta: str | None = None):
    res = await send_message(chat_id, text_md, reply_markup=reply_markup, parse_mode="Markdown")
    if chat_id != ADMIN_CHAT_ID:
        ok = bool(res.get("ok"))
        status = "ОТПРАВЛЕНО" if ok else f"ОШИБКА: {escape_html(safe_str(res))}"
        meta_txt = f"<b>{escape_html(meta)}</b><br/>" if meta else ""
        admin_text = (
            f"{meta_txt}<b>➡️ Исходящее клиенту</b><br/>"
            f"chat_id: <code>{chat_id}</code><br/>"
            f"Статус: <b>{status}</b><br/><br/>"
            f"{escape_html(text_md)[:3500]}"
        )
        await notify_admin(admin_text)
    return res

# ------------------- UI -------------------
def main_menu():
    return inline_keyboard([
        [{"text": "📅 Онлайн-запись", "url": ONLINE_BOOKING_URL}],
        [{"text": "💬 Написать администратору", "callback_data": "menu:to_admin"}],
        [{"text": "📱 Привязать номер", "callback_data": "menu:link_phone"}],
    ])

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

# ------------------- ШАБЛОНЫ -------------------
ADDRESS_BLOCK = (
    "Aдрес cтудии\n"
    "ул. Фасаднaя, д. 21\n\n"
    "Вхoд сo стороны улицы Фacaдная\n"
    "Яндeкc.Карты\n"
    "https://kutikula116.clients.site"
)

def tpl_booking_created(service: str, master: str, price: str, dt_str: str) -> str:
    return (
        "👋 Вы записaны в \n"
        "Studio KUTIKULA \n\n"
        f"▫️{service}\n"
        f"{master}\n"
        f"{price}\n"
        f"{dt_str}\n\n"
        f"{ADDRESS_BLOCK}\n\n"
        "Ждём Bаc!"
    )

def tpl_reminder(dt_line: str, service: str) -> str:
    header = "Дoбрый вeчер!\nHа cвязи Nail Studio KUTIKULA\n\n"
    return (
        f"{header}"
        "Hапоминaем, чтo Вы записaны\n"
        f"*{dt_line}*\n"
        f"▫️{service}\n\n"
        "Aдрec cтyдии:\n"
        "yл. Фacаднaя, 21\n"
        "_вxoд cо стoрoны yл. Фaсадной_\n\n"
        "Cсылка на Яндекc.Kaрты:\n"
        "https://kutikula116.clients.site\n\n"
        "*Пожалyйcтa, отправьтe:*\n"
        "*«+» — если пoдтверждаeте визит*\n"
        "*«–» — eсли xотитe oтмeнить или перeнеcти запись*"
    )

def tpl_cancel(service: str, dt_line: str) -> str:
    return (
        "Вaша зaпиcь\n"
        f"▫️{service}\n"
        f"нa {dt_line} oтмeнeнa.\n\n"
        "Вы мoжeте выбрать удобнoе для себя врeмя, вocпoльзовaвшиcь онлaйн-зaписью пeрeйдя по ccылкe:\n"
        f"*{ONLINE_BOOKING_URL}*"
    )

def tpl_reschedule(service: str, old_dt: str, new_dt: str) -> str:
    return (
        "Ваша запись изменена.\n"
        f"▫️{service}\n\n"
        f"Было: *{old_dt}*\n"
        f"Стало: *{new_dt}*\n\n"
        "Если нужно перенести или отменить — можно сделать это через онлайн-запись:\n"
        f"*{ONLINE_BOOKING_URL}*"
    )

# ------------------- REMINDERS STORAGE -------------------
def reminders_load() -> dict:
    return _load_json(REMINDERS_FILE)

def reminders_save(data: dict):
    _save_json(REMINDERS_FILE, data)

def reminders_upsert(record_id: str, payload: dict):
    data = reminders_load()
    data[record_id] = payload
    reminders_save(data)

def reminders_get(record_id: str) -> dict | None:
    data = reminders_load()
    return data.get(record_id)

def reminders_delete(record_id: str):
    data = reminders_load()
    if record_id in data:
        del data[record_id]
        reminders_save(data)

# ------------------- YCLIENTS WEBHOOK PARSERS -------------------
def extract_from_yclients_webhook(payload: dict) -> dict:
    d = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    status = safe_str(payload.get("status") or d.get("status") or "").lower().strip()
    record_id = payload.get("resource_id") or d.get("id") or d.get("record_id")
    record_id = safe_str(record_id)
    company_id = payload.get("company_id") or d.get("company_id") or YCLIENTS_COMPANY_ID
    try:
        company_id = int(company_id)
    except Exception:
        company_id = int(YCLIENTS_COMPANY_ID)

    phone_raw = None
    if isinstance(d.get("client"), dict):
        phone_raw = d["client"].get("phone") or d["client"].get("phone_number")
    phone_raw = phone_raw or d.get("phone") or d.get("client_phone")
    phone = normalize_phone(safe_str(phone_raw)) or safe_str(phone_raw)

    start_str = d.get("start_at") or d.get("datetime") or d.get("date")
    start_dt = try_parse_dt(start_str) if start_str else None

    return {"status": status, "record_id": record_id, "company_id": company_id, "phone": phone, "start_dt": start_dt, "raw": payload}

def _pick_service_and_price(rec: dict) -> tuple[str, str]:
    services = rec.get("services")
    if isinstance(services, list) and services:
        s0 = services[0]
        if isinstance(s0, dict):
            title = first_non_empty(s0.get("title"), s0.get("name"), s0.get("label"))
            price = first_non_empty(s0.get("price"), s0.get("cost"), s0.get("amount"), s0.get("sum"))
            return title, price
        if isinstance(s0, str):
            return s0, ""
    service = rec.get("service")
    if isinstance(service, dict):
        title = first_non_empty(service.get("title"), service.get("name"))
        price = first_non_empty(service.get("price"), service.get("cost"), service.get("amount"), service.get("sum"))
        return title, price
    title = first_non_empty(rec.get("service_title"), rec.get("services_titles"), rec.get("title"))
    price = first_non_empty(rec.get("price"), rec.get("cost"), rec.get("amount"), rec.get("sum"), rec.get("total"), rec.get("total_cost"))
    return title, price

def _pick_master(rec: dict) -> str:
    staff = rec.get("staff")
    if isinstance(staff, dict):
        return first_non_empty(staff.get("name"), staff.get("title"), staff.get("full_name"))
    if isinstance(staff, list) and staff:
        s0 = staff[0]
        if isinstance(s0, dict):
            return first_non_empty(s0.get("name"), s0.get("title"), s0.get("full_name"))
        if isinstance(s0, str):
            return s0
    return first_non_empty(rec.get("staff_name"), rec.get("master"), rec.get("master_name"))

def extract_from_record_detail(rec: dict) -> dict:
    phone_raw = None
    if isinstance(rec.get("client"), dict):
        phone_raw = rec["client"].get("phone") or rec["client"].get("phone_number")
    phone_raw = phone_raw or rec.get("client_phone") or rec.get("phone")
    phone = normalize_phone(safe_str(phone_raw)) or safe_str(phone_raw)

    start_str = first_non_empty(rec.get("datetime"), rec.get("date"), rec.get("start_at"), rec.get("start"))
    start_dt = try_parse_dt(start_str) if start_str else None

    service, price = _pick_service_and_price(rec)
    master = _pick_master(rec)

    return {"phone": phone, "start_dt": start_dt, "service": safe_str(service), "master": safe_str(master), "price": safe_str(price)}

def fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return dt.strftime("%d.%m.%Y %H:%M")

# ------------------- REMINDER LOOP -------------------
async def reminder_loop():
    await asyncio.sleep(2)
    while True:
        try:
            data = reminders_load()
            if not data:
                await asyncio.sleep(60)
                continue

            now = _now_studio()
            changed = False

            for rid, r in list(data.items()):
                try:
                    chat_id = int(r.get("chat_id") or 0)
                    if not chat_id:
                        continue

                    start_iso = r.get("start_dt")
                    start_dt = try_parse_dt(start_iso) if isinstance(start_iso, str) else None
                    if not start_dt:
                        continue

                    service = md_sanitize(r.get("service") or "УСЛУГА")
                    sent = r.get("sent", {}) or {}

                    t3 = start_dt - timedelta(days=3)
                    t1 = start_dt - timedelta(days=1)
                    t2h = start_dt - timedelta(hours=2)

                    if now >= t3 and not sent.get("t-3d"):
                        await send_client(chat_id, tpl_reminder(fmt_dt(start_dt), service), meta="REMINDER_3D")
                        sent["t-3d"] = True
                        changed = True

                    if now >= t1 and not sent.get("t-1d"):
                        await send_client(chat_id, tpl_reminder(fmt_dt(start_dt), service), meta="REMINDER_1D")
                        sent["t-1d"] = True
                        changed = True

                    if now >= t2h and not sent.get("t-2h"):
                        msg = (
                            f"⏳ Ждём Baс в *{start_dt.strftime('%H:%M')}*\n\n"
                            "*Пoжaлyйстa, отпрaвьтe:*\n"
                            "*«+» — еcли пoдтверждaетe визит*\n"
                            "*«–» — ecли xотитe oтменить или пeрeнеcти зaпись*"
                        )
                        await send_client(chat_id, msg, meta="REMINDER_2H")
                        sent["t-2h"] = True
                        changed = True

                    if now > start_dt + timedelta(hours=6):
                        del data[rid]
                        changed = True
                        continue

                    r["sent"] = sent
                    data[rid] = r

                except Exception as e:
                    logger.error(f"reminder_loop record error: {e}")

            if changed:
                reminders_save(data)

        except Exception as e:
            logger.error(f"reminder_loop error: {e}")

        await asyncio.sleep(60)

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(reminder_loop())

# ------------------- YCLIENTS WEBHOOK -------------------
@app.post("/yclients-webhook")
async def yclients_webhook(request: Request):
    secret_q = request.query_params.get("secret", "")
    secret_h = request.headers.get("X-Webhook-Secret", "")
    incoming = secret_q or secret_h
    if YCLIENTS_WEBHOOK_SECRET and incoming != YCLIENTS_WEBHOOK_SECRET:
        return JSONResponse(status_code=403, content={"ok": False, "error": "forbidden"})

    payload = await request.json()
    logger.info(f"YCLIENTS webhook: {payload}")

    f = extract_from_yclients_webhook(payload)
    status = f["status"]
    record_id = f["record_id"] or ""

    is_create = status in {"create", "created", "new"}
    is_update = status in {"update", "updated", "edit", "edited", "change", "changed"}
    is_delete = status in {"delete", "deleted", "cancel", "canceled", "cancelled", "remove", "removed"}

    if not (is_create or is_update or is_delete):
        return {"ok": True}

    details = {"phone": f["phone"], "start_dt": f["start_dt"], "service": "", "master": "", "price": ""}

    rec = None
    if get_record_by_id and record_id and (is_create or is_update):
        try:
            rec = await get_record_by_id(f["company_id"], record_id)
            if rec:
                det2 = extract_from_record_detail(rec)
                for k, v in det2.items():
                    if (not details.get(k)) and v:
                        details[k] = v
        except Exception as e:
            logger.error(f"get_record_by_id failed: {e}")

    phone = details["phone"]
    if not phone:
        await notify_admin(
            f"<b>YCLIENTS webhook</b><br/>record_id: <code>{escape_html(record_id)}</code><br/>"
            "Не нашла телефон (ни в webhook, ни в деталях записи)."
        )
        return {"ok": True}

    chat_id = phone_to_chat_map().get(str(phone))
    if not chat_id:
        await notify_admin(
            f"<b>Событие записи (YCLIENTS)</b><br/>status: <code>{escape_html(status)}</code><br/>"
            f"record_id: <code>{escape_html(record_id)}</code><br/>"
            f"Телефон: <code>{escape_html(phone)}</code><br/>"
            "Клиент не привязан к боту (не отправлял номер)."
        )
        return {"ok": True}

    # ОТМЕНА
    if is_delete:
        service_txt = md_sanitize(details["service"] or "УСЛУГА")
        dt_line = fmt_dt(details["start_dt"])
        await send_client(chat_id, tpl_cancel(service_txt, dt_line), meta="BOOKING_CANCEL")
        if record_id:
            reminders_delete(record_id)
        await notify_admin(f"<b>❌ Отмена</b><br/>record_id: <code>{escape_html(record_id)}</code><br/>chat_id: <code>{chat_id}</code>")
        return {"ok": True}

    # СОЗДАНИЕ
    if is_create:
        dt_line = f"Дата и время визита: {fmt_dt(details['start_dt'])}"
        service_txt = md_sanitize(details["service"] or "УСЛУГА")
        master_txt = md_sanitize(details["master"]) if details["master"] else "к какому Мастеру: —"
        price_txt = f"Предварительная стоимость: {md_sanitize(details['price'])}" if details["price"] else "Предварительная стоимость: —"

        await send_client(chat_id, tpl_booking_created(service_txt, master_txt, price_txt, dt_line), meta="BOOKING_CREATED")

        if record_id and details["start_dt"]:
            reminders_upsert(record_id, {
                "phone": phone,
                "chat_id": chat_id,
                "start_dt": details["start_dt"].strftime("%Y-%m-%d %H:%M:%S"),
                "service": details["service"] or "УСЛУГА",
                "master": details["master"] or "",
                "price": details["price"] or "",
                "sent": {},
            })
        return {"ok": True}

    # ИЗМЕНЕНИЕ / ПЕРЕНОС
    if is_update:
        prev = reminders_get(record_id) if record_id else None
        prev_dt = try_parse_dt(prev.get("start_dt")) if prev and prev.get("start_dt") else None
        new_dt = details["start_dt"]

        service_txt = md_sanitize(details["service"] or (prev.get("service") if prev else "") or "УСЛУГА")

        if not new_dt:
            if record_id and prev:
                prev["service"] = details["service"] or prev.get("service") or "УСЛУГА"
                prev["master"] = details["master"] or prev.get("master") or ""
                prev["price"] = details["price"] or prev.get("price") or ""
                reminders_upsert(record_id, prev)
            return {"ok": True}

        if prev_dt and (fmt_dt(prev_dt) != fmt_dt(new_dt)):
            await send_client(chat_id, tpl_reschedule(service_txt, fmt_dt(prev_dt), fmt_dt(new_dt)), meta="BOOKING_RESCHEDULE")
            if record_id:
                reminders_upsert(record_id, {
                    "phone": phone,
                    "chat_id": chat_id,
                    "start_dt": new_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "service": details["service"] or (prev.get("service") if prev else "УСЛУГА") or "УСЛУГА",
                    "master": details["master"] or (prev.get("master") if prev else "") or "",
                    "price": details["price"] or (prev.get("price") if prev else "") or "",
                    "sent": {},  # заново
                })
            return {"ok": True}

        if record_id:
            reminders_upsert(record_id, {
                "phone": phone,
                "chat_id": chat_id,
                "start_dt": new_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "service": details["service"] or (prev.get("service") if prev else "УСЛУГА") or "УСЛУГА",
                "master": details["master"] or (prev.get("master") if prev else "") or "",
                "price": details["price"] or (prev.get("price") if prev else "") or "",
                "sent": (prev.get("sent") if prev else {}) or {},
            })
        return {"ok": True}

    return {"ok": True}

# ------------------- TELEGRAM WEBHOOK -------------------
@app.get("/")
async def root():
    return {"status": "ok"}

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    message = update.get("message")
    if not message and "callback_query" not in update:
        return JSONResponse(content={"ok": True})

    # callbacks
    if "callback_query" in update:
        cq = update["callback_query"]
        chat_id = cq.get("message", {}).get("chat", {}).get("id")
        data = cq.get("data", "")
        await tg_post("answerCallbackQuery", {"callback_query_id": cq.get("id")})

        if data == "menu:to_admin":
            st = get_state(chat_id)
            set_state(chat_id, "chat_to_admin", st.get("data", {}))
            await send_client(chat_id, "Напишите сообщение — я перешлю администратору.", meta="TO_ADMIN")
            return JSONResponse(content={"ok": True})

        if data == "menu:link_phone":
            st = get_state(chat_id)
            set_state(chat_id, "await_contact", st.get("data", {}))
            await send_client(
                chat_id,
                "Нажмите кнопку ниже, чтобы отправить номер телефона (нужно для напоминаний о записи).",
                reply_markup={"keyboard": [[{"text": "📱 Отправить номер", "request_contact": True}]],
                              "resize_keyboard": True, "one_time_keyboard": True},
                meta="LINK_PHONE",
            )
            return JSONResponse(content={"ok": True})

        return JSONResponse(content={"ok": True})

    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()

    # контакт
    contact = message.get("contact")
    if contact:
        phone = normalize_phone(contact.get("phone_number", "")) or contact.get("phone_number", "")
        st = get_state(chat_id)
        data_mem = st.get("data", {})
        data_mem["phone"] = phone
        set_state(chat_id, "idle", data_mem)
        await notify_admin(f"<b>📱 Клиент отправил контакт</b><br/>chat_id: <code>{chat_id}</code><br/>тел: <code>{escape_html(phone)}</code>")
        await send_client(chat_id, "Спасибо! Номер сохранён.", reply_markup=main_menu(), meta="CONTACT_SAVED")
        return JSONResponse(content={"ok": True})

    # привет
    if text.lower() in ("/start", "start", "привет", "здравствуйте", "добрый день", "добрый вечер"):
        await show_welcome(chat_id)
        return JSONResponse(content={"ok": True})

    # номер текстом
    ph = normalize_phone(text)
    if ph:
        st = get_state(chat_id)
        data_mem = st.get("data", {})
        data_mem["phone"] = ph
        set_state(chat_id, "idle", data_mem)
        await notify_admin(f"<b>📱 Клиент прислал номер текстом</b><br/>chat_id: <code>{chat_id}</code><br/>тел: <code>{escape_html(ph)}</code>")
        await send_client(chat_id, "Спасибо! Номер сохранён.", reply_markup=main_menu(), meta="PHONE_SAVED_TEXT")
        return JSONResponse(content={"ok": True})

    # подтверждение/отмена клиентом
    if text in ("+", "＋"):
        await notify_admin(f"<b>✅ Подтверждение визита</b><br/>chat_id: <code>{chat_id}</code>")
        await send_client(chat_id, "Спасибо! Визит подтверждён ✅", reply_markup=main_menu(), meta="CLIENT_CONFIRM")
        return JSONResponse(content={"ok": True})
    if text in ("-", "–", "—"):
        await notify_admin(f"<b>❗️Клиент хочет отменить/перенести</b><br/>chat_id: <code>{chat_id}</code>")
        await send_client(chat_id, "Поняла. Я передала администратору — мы свяжемся с вами 🙌", reply_markup=main_menu(), meta="CLIENT_CANCEL_REQUEST")
        return JSONResponse(content={"ok": True})

    # сообщение админу
    st = get_state(chat_id)
    if st.get("step") == "chat_to_admin":
        await notify_admin(f"<b>📩 Входящее от клиента</b><br/>chat_id: <code>{chat_id}</code><br/>{escape_html(text)[:3500]}")
        await send_client(chat_id, "Сообщение передано администратору. Ответим вам в этом чате.", reply_markup=main_menu(), meta="MSG_TO_ADMIN_OK")
        set_state(chat_id, "idle", st.get("data", {}))
        return JSONResponse(content={"ok": True})

    # дефолт
    await send_client(
        chat_id,
        "Принято.\n\nЧтобы записаться — нажмите «Онлайн-запись».\nЕсли нужен администратор — нажмите «Написать администратору».",
        reply_markup=main_menu(),
        meta="DEFAULT_REPLY",
    )
    return JSONResponse(content={"ok": True})
