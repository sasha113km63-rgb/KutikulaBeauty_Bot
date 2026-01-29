# -*- coding: utf-8 -*-
"""
KUTIKULA Beauty Assistant — Telegram bot + YCLIENTS webhooks

Функции:
- Telegram:
  - приветствие (/start, привет, здравствуйте...)
  - кнопки: Онлайн-запись, Написать администратору, Привязать номер
  - привязка телефона (contact или текстом)
  - ответы клиента: "+" подтверждение, "-" запрос отмены/переноса (уведомление админу)
  - дублирование входящих сообщений клиент->админ

- YCLIENTS:
  - webhook /yclients-webhook?secret=... (create/update/delete)
  - "отбивка" при создании записи (шаблон "Вы записаны...")
  - "отбивка" при отмене (delete/cancel)
  - "отбивка" при переносе/изменении времени (update с изменением datetime)
  - планировщик напоминаний (3 дня, 1 день, 2 часа) — отправляет только будущие напоминания

Хранение (в файловой системе сервиса):
- storage_phone_map.json  — phone -> chat_id
- storage_records.json    — record_id -> {"datetime": "...", "phone": "...", ...}
- storage_reminders.json  — список будущих напоминаний
- storage_sent.json       — дедупликация отправок (record_id + type)

ENV:
TELEGRAM_TOKEN
ADMIN_CHAT_ID
ONLINE_BOOKING_URL (если нет — берём https://n561655.yclients.com/)
YCLIENTS_COMPANY_ID
YCLIENTS_WEBHOOK_SECRET (строка из query param secret=...)
(+ ваши остальные переменные остаются как есть)
"""
import os
import json
import re
import logging
import html
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, List, Tuple

import aiohttp
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from yclients_api import get_record_by_id

# ----------------- CONFIG -----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
ONLINE_BOOKING_URL = os.getenv("ONLINE_BOOKING_URL", "https://n561655.yclients.com/").strip()

YCLIENTS_COMPANY_ID = os.getenv("YCLIENTS_COMPANY_ID", "").strip()
YCLIENTS_WEBHOOK_SECRET = os.getenv("YCLIENTS_WEBHOOK_SECRET", "").strip()

# storage files
PHONE_MAP_FILE = "storage_phone_map.json"
STATE_FILE = "storage_state.json"          # optional (steps)
RECORDS_FILE = "storage_records.json"      # record_id -> last known details
REMINDERS_FILE = "storage_reminders.json"  # future reminders queue
SENT_FILE = "storage_sent.json"            # dedupe (record_id:type)

# timing
TICK_SECONDS = int(os.getenv("TICK_SECONDS", "30"))
# do not send reminders that are already in the past by more than this
PAST_GRACE_SECONDS = int(os.getenv("PAST_GRACE_SECONDS", "60"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kutikula_bot")

app = FastAPI()
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ----------------- HELPERS -----------------
def safe_str(x: Any) -> str:
    return "" if x is None else str(x)

def escape_html(s: str) -> str:
    return html.escape(s or "")

def md_escape(s: str) -> str:
    # basic MarkdownV2 is painful; we use Markdown (classic) and keep it simple
    return (s or "").replace("*", "").replace("_", "").replace("`", "")

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def normalize_phone(s: str) -> str:
    s = safe_str(s)
    digits = re.sub(r"\D+", "", s)
    if not digits:
        return ""
    # RU common
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    if digits.startswith("7") and len(digits) == 11:
        return "+" + digits
    if digits.startswith("+") and len(digits) >= 11:
        return digits
    return "+" + digits if not digits.startswith("+") else digits

def try_parse_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    s = safe_str(s).strip()
    # handle "Z"
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        # iso with offset
        if "T" in s and ("+" in s or s.count(":") >= 2):
            return datetime.fromisoformat(s)
    except Exception:
        pass
    # common formats
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M"):
        try:
            # assume local time is UTC+4 (Samara) if no tz
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone(timedelta(hours=4)))
        except Exception:
            continue
    return None

def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def phone_map() -> Dict[str, int]:
    return {k: int(v) for k, v in load_json(PHONE_MAP_FILE, {}).items()}

def set_phone_map(phone: str, chat_id: int) -> None:
    m = load_json(PHONE_MAP_FILE, {})
    m[str(phone)] = int(chat_id)
    save_json(PHONE_MAP_FILE, m)

def get_state(chat_id: int) -> Dict[str, Any]:
    st = load_json(STATE_FILE, {})
    return st.get(str(chat_id), {"step": "idle", "data": {}})

def set_state(chat_id: int, step: str, data: Optional[Dict[str, Any]] = None) -> None:
    st = load_json(STATE_FILE, {})
    st[str(chat_id)] = {"step": step, "data": data or {}}
    save_json(STATE_FILE, st)

def sent_key(record_id: str, kind: str) -> str:
    return f"{record_id}:{kind}"

def was_sent(record_id: str, kind: str) -> bool:
    s = load_json(SENT_FILE, {})
    return sent_key(record_id, kind) in s

def mark_sent(record_id: str, kind: str, extra: Optional[Dict[str, Any]] = None) -> None:
    s = load_json(SENT_FILE, {})
    s[sent_key(record_id, kind)] = {"ts": now_utc().isoformat(), **(extra or {})}
    save_json(SENT_FILE, s)

def records_store() -> Dict[str, Any]:
    return load_json(RECORDS_FILE, {})

def save_record(record_id: str, data: Dict[str, Any]) -> None:
    store = load_json(RECORDS_FILE, {})
    store[str(record_id)] = data
    save_json(RECORDS_FILE, store)

# ----------------- TELEGRAM SEND -----------------
async def tg_post(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not TELEGRAM_TOKEN:
        return {"ok": False, "error": "TELEGRAM_TOKEN empty"}
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{TELEGRAM_API}/{method}", json=payload, timeout=20) as r:
            try:
                data = await r.json()
            except Exception:
                data = {"ok": False, "status": r.status, "text": await r.text()}
            return data

async def send_client(chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None, parse_mode: str = "HTML") -> None:
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    await tg_post("sendMessage", payload)

async def notify_admin(text: str) -> None:
    if not ADMIN_CHAT_ID:
        return
    try:
        await send_client(int(ADMIN_CHAT_ID), text, parse_mode="HTML")
    except Exception as e:
        logger.exception(f"notify_admin failed: {e}")

def main_menu() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "📅 Онлайн-запись", "url": ONLINE_BOOKING_URL}],
            [{"text": "💬 Написать администратору", "callback_data": "menu:to_admin"}],
            [{"text": "📱 Привязать номер", "callback_data": "menu:link_phone"}],
        ]
    }

# ----------------- TEMPLATES -----------------
WELCOME_TEXT = (
    "Здравствуйте 🌸\n"
    "Я — виртуальный администратор студии KUTIKULA.\n\n"
    "Я могу присылать вам напоминание о Вашей записи. За три дня, за один день и за пару часов до записи.\n\n"
    "Изменить запись вы сможете с помощью онлайн записи перейдя по ссылке:\n"
    f"{ONLINE_BOOKING_URL}"
)

def tpl_booking_created(service: str, master: str, price_line: str, dt_line: str) -> str:
    return (
        "👋 Вы записaны в\n"
        "Studio KUTIKULA\n\n"
        f"▫️ {service}\n"
        f"{master}\n"
        f"{price_line}\n"
        f"{dt_line}\n\n"
        "Aдрес cтудии\n"
        "ул. Фасаднaя, д. 21\n\n"
        "Вхoд сo стороны улицы Фacaдная\n"
        "Яндeкc.Карты\n"
        "https://kutikula116.clients.site\n\n"
        "Ждём Bаc!"
    )

def tpl_reminder_3d(dt_human: str, time_human: str, service: str) -> str:
    return (
        "Дoбрый вeчер!\n"
        "Hа cвязи Nail Studio KUTIKULA\n\n"
        "Hапоминaем, чтo Вы записaны\n"
        f"<b>{escape_html(dt_human)},</b>\n"
        f"<b>нa {escape_html(time_human)}</b>\n"
        f"▫️ {escape_html(service)}\n\n"
        "Aдрec cтyдии:\n"
        "yл. Фacаднaя, 21\n"
        "<i>вxoд cо стoрoны yл. Фaсадной</i>\n\n"
        "Cсылка на Яндекc.Kaрты:\n"
        "https://kutikula116.clients.site\n\n"
        "<b>Пожалyйcтa, отправьтe:</b>\n"
        "<b>«+» — если пoдтверждаeте визит</b>\n"
        "<b>«–» — eсли xотитe oтмeнить или перeнеcти запись</b>"
    )

def tpl_reminder_1d(dt_human: str, time_human: str, service: str) -> str:
    # same text style, but could be tweaked later
    return tpl_reminder_3d(dt_human, time_human, service)

def tpl_reminder_2h(time_human: str) -> str:
    return (
        f"⏳ Ждём Baс в <b>{escape_html(time_human)}</b>\n\n"
        "<b>Пoжaлyйстa, отпрaвьтe:</b>\n"
        "<b>«+» — еcли пoдтверждaетe визит</b>\n"
        "<b>«–» — ecли xотитe oтменить или пeрeнеcти зaпись</b>"
    )

def tpl_cancelled(dt_human: str, time_human: str, service: str) -> str:
    return (
        "Вaша зaпиcь\n"
        f"▫️{escape_html(service)}\n"
        f"нa {escape_html(dt_human)} в {escape_html(time_human)} oтмeнeнa.\n\n"
        "Вы мoжeте выбрать удобнoе для себя врeмя, вocпoльзовaвшиcь онлaйн-зaписью пeрeйдя по ccылкe:\n"
        f"<b>{escape_html(ONLINE_BOOKING_URL)}</b>"
    )

def tpl_rescheduled(old_dt: str, old_time: str, new_dt: str, new_time: str, service: str) -> str:
    return (
        "Ваша запись перенесена:\n"
        f"▫️{escape_html(service)}\n\n"
        f"Было: <b>{escape_html(old_dt)} {escape_html(old_time)}</b>\n"
        f"Стало: <b>{escape_html(new_dt)} {escape_html(new_time)}</b>\n\n"
        "Если нужно изменить ещё раз — используйте онлайн-запись:\n"
        f"<b>{escape_html(ONLINE_BOOKING_URL)}</b>"
    )

# ----------------- REMINDERS QUEUE -----------------
def load_reminders() -> List[Dict[str, Any]]:
    return load_json(REMINDERS_FILE, [])

def save_reminders(items: List[Dict[str, Any]]) -> None:
    save_json(REMINDERS_FILE, items)

def reminder_id(record_id: str, kind: str) -> str:
    return f"{record_id}:{kind}"

def upsert_reminder(record_id: str, chat_id: int, when_dt: datetime, kind: str, payload: Dict[str, Any]) -> None:
    """Add/replace reminder only if it's in the future (with small grace)."""
    now = now_utc()
    when_utc = when_dt.astimezone(timezone.utc)
    if when_utc <= now + timedelta(seconds=PAST_GRACE_SECONDS):
        # do not enqueue past reminders
        return

    items = load_reminders()
    rid = reminder_id(record_id, kind)
    new_item = {
        "id": rid,
        "record_id": str(record_id),
        "chat_id": int(chat_id),
        "kind": kind,
        "when": when_utc.isoformat(),
        "payload": payload,
        "sent": False,
    }
    # replace if exists
    items = [x for x in items if x.get("id") != rid]
    items.append(new_item)
    save_reminders(items)

def delete_reminders_for_record(record_id: str) -> None:
    items = load_reminders()
    items = [x for x in items if x.get("record_id") != str(record_id)]
    save_reminders(items)

async def reminders_worker() -> None:
    """Background task that sends due reminders once."""
    while True:
        try:
            items = load_reminders()
            if not items:
                await asyncio.sleep(TICK_SECONDS)
                continue

            now = now_utc()
            changed = False
            for it in items:
                if it.get("sent"):
                    continue
                when = try_parse_dt(it.get("when", ""))
                if not when:
                    it["sent"] = True
                    changed = True
                    continue
                when_utc = when.astimezone(timezone.utc)
                if when_utc <= now:
                    record_id = safe_str(it.get("record_id"))
                    kind = safe_str(it.get("kind"))
                    # dedupe at storage_sent level too
                    if record_id and kind and not was_sent(record_id, f"rem_{kind}"):
                        try:
                            await send_client(int(it["chat_id"]), it["payload"]["text"], reply_markup=main_menu(), parse_mode=it["payload"].get("parse_mode", "HTML"))
                            mark_sent(record_id, f"rem_{kind}", {"chat_id": it["chat_id"]})
                            await notify_admin(f"<b>⏰ Напоминание отправлено</b><br/>chat_id: <code>{it['chat_id']}</code><br/>record_id: <code>{escape_html(record_id)}</code><br/>тип: <code>{escape_html(kind)}</code>")
                        except Exception as e:
                            logger.exception(f"send reminder failed: {e}")
                    it["sent"] = True
                    changed = True

            # cleanup old sent reminders
            cleaned: List[Dict[str, Any]] = []
            for it in items:
                when = try_parse_dt(it.get("when", ""))
                if it.get("sent") and when:
                    # keep a little, then drop
                    if when.astimezone(timezone.utc) < now - timedelta(days=1):
                        changed = True
                        continue
                cleaned.append(it)
            if changed:
                save_reminders(cleaned)

        except Exception as e:
            logger.exception(f"reminders_worker loop error: {e}")

        await asyncio.sleep(TICK_SECONDS)

@app.on_event("startup")
async def _startup():
    # start scheduler
    asyncio.create_task(reminders_worker())

# ----------------- YCLIENTS WEBHOOK PARSING -----------------
def parse_yclients_payload(payload: Dict[str, Any]) -> Tuple[str, str, int, Dict[str, Any]]:
    """
    Return (status, record_id, company_id, raw_data)
    """
    status = safe_str(payload.get("status") or payload.get("event") or payload.get("type") or "").lower()
    record_id = ""
    company_id = 0

    # yclients often: {"company_id":..., "resource":"record", "resource_id":..., "status":"create|update|delete", "data":{...}}
    if payload.get("resource_id") is not None:
        record_id = safe_str(payload.get("resource_id"))
    if payload.get("record_id") is not None:
        record_id = safe_str(payload.get("record_id"))
    if isinstance(payload.get("data"), dict) and payload["data"].get("id") is not None:
        record_id = safe_str(payload["data"].get("id"))

    if payload.get("company_id") is not None:
        company_id = int(payload.get("company_id"))
    elif payload.get("company") is not None:
        company_id = int(payload.get("company"))
    elif YCLIENTS_COMPANY_ID:
        try:
            company_id = int(YCLIENTS_COMPANY_ID)
        except Exception:
            company_id = 0

    # normalize status variants
    if status in ("created", "new"):
        status = "create"
    if status in ("removed", "canceled", "cancelled"):
        status = "delete"

    return status, record_id, company_id, payload

def record_details_from_api(rec: Dict[str, Any]) -> Dict[str, Any]:
    """
    rec = yclients record object (from get_record_by_id)
    Returns dict: phone, dt (aware), service, master, price
    """
    phone_raw = ""
    if isinstance(rec.get("client"), dict):
        phone_raw = rec["client"].get("phone") or rec["client"].get("phone_number") or ""
    phone_raw = phone_raw or rec.get("client_phone") or rec.get("phone") or ""
    phone = normalize_phone(phone_raw) or safe_str(phone_raw)

    dt_raw = rec.get("datetime") or rec.get("date") or rec.get("start_at") or ""
    dt = try_parse_dt(safe_str(dt_raw))

    service = ""
    price = ""
    if isinstance(rec.get("services"), list) and rec["services"]:
        s0 = rec["services"][0]
        if isinstance(s0, dict):
            service = safe_str(s0.get("title") or s0.get("name") or "")
            if s0.get("price") is not None:
                price = safe_str(s0.get("price"))

    master = ""
    if isinstance(rec.get("staff"), dict):
        master = safe_str(rec["staff"].get("name") or "")

    return {"phone": phone, "dt": dt, "service": service, "master": master, "price": price}

def human_dt(dt: datetime) -> Tuple[str, str]:
    # show in local timezone (UTC+4)
    local = dt.astimezone(timezone(timedelta(hours=4)))
    dt_h = local.strftime("%d.%m.%Y")
    time_h = local.strftime("%H:%M")
    return dt_h, time_h

def schedule_all_reminders(record_id: str, chat_id: int, dt: datetime, service: str) -> None:
    """
    Create future reminders:
    - 3 days
    - 1 day
    - 2 hours
    """
    dt_h, time_h = human_dt(dt)
    # 3 days
    upsert_reminder(
        record_id, chat_id, dt - timedelta(days=3), "3d",
        {"text": tpl_reminder_3d(dt_h, time_h, service), "parse_mode": "HTML"}
    )
    # 1 day
    upsert_reminder(
        record_id, chat_id, dt - timedelta(days=1), "1d",
        {"text": tpl_reminder_1d(dt_h, time_h, service), "parse_mode": "HTML"}
    )
    # 2 hours
    upsert_reminder(
        record_id, chat_id, dt - timedelta(hours=2), "2h",
        {"text": tpl_reminder_2h(time_h), "parse_mode": "HTML"}
    )

# ----------------- ROUTES -----------------
@app.get("/")
async def root():
    return {"status": "ok"}

@app.post("/yclients-webhook")
async def yclients_webhook(request: Request):
    # secret check
    incoming = request.query_params.get("secret") or request.headers.get("X-Webhook-Secret") or ""
    if YCLIENTS_WEBHOOK_SECRET and incoming != YCLIENTS_WEBHOOK_SECRET:
        return JSONResponse(status_code=403, content={"ok": False, "error": "forbidden"})

    payload = await request.json()
    status, record_id, company_id, _raw = parse_yclients_payload(payload)

    logger.info(f"YCLIENTS webhook status={status} record_id={record_id} company_id={company_id}")

    if not record_id:
        await notify_admin(f"<b>YCLIENTS webhook</b><br/>Не смогла определить record_id.<br/><pre>{escape_html(safe_str(payload)[:2000])}</pre>")
        return {"ok": True}

    # Always fetch record details for create/update (and sometimes delete might still be available)
    rec = None
    if status in ("create", "update", "delete"):
        try:
            rec = await get_record_by_id(company_id or int(YCLIENTS_COMPANY_ID or "0"), record_id)
        except Exception as e:
            logger.exception(f"get_record_by_id failed: {e}")
            rec = None

    details = record_details_from_api(rec) if isinstance(rec, dict) else {"phone": "", "dt": None, "service": "", "master": "", "price": ""}
    phone = details.get("phone") or ""
    dt = details.get("dt")
    service = details.get("service") or "УСЛУГА"
    master = details.get("master") or "к какому Мастеру"
    price = details.get("price") or ""
    price_line = f"Предварительная cтoимoсть: {md_escape(price)}" if price else "Предварительная cтoимoсть: —"

    # mapping phone -> chat
    chat_id = None
    if phone:
        chat_id = phone_map().get(str(phone))

    # store previous (for reschedule comparison)
    prev = records_store().get(str(record_id), {})
    prev_dt = try_parse_dt(prev.get("datetime", "")) if isinstance(prev, dict) else None

    # update stored record snapshot
    save_record(str(record_id), {
        "phone": phone,
        "datetime": dt.isoformat() if dt else "",
        "service": service,
        "master": master,
        "price": price,
        "updated_at": now_utc().isoformat(),
    })

    # If no chat link — notify admin and stop
    if not chat_id:
        await notify_admin(
            "<b>YCLIENTS событие</b><br/>"
            f"status: <code>{escape_html(status)}</code><br/>"
            f"record_id: <code>{escape_html(record_id)}</code><br/>"
            f"тел: <code>{escape_html(phone or '—')}</code><br/>"
            "Клиент не привязан к боту (не отправлял номер)."
        )
        # still maintain reminders cleanup on delete/update
        if status == "delete":
            delete_reminders_for_record(record_id)
        if status == "update" and dt:
            delete_reminders_for_record(record_id)
        return {"ok": True}

    # --- handle events ---
    if status == "create":
        if not was_sent(record_id, "created"):
            dt_line = f"Дата и время визита: {dt.astimezone(timezone(timedelta(hours=4))).strftime('%d.%m.%Y %H:%M')}" if dt else "Дата и время визита: уточните у администратора"
            msg = tpl_booking_created(md_escape(service), md_escape(master), md_escape(price_line), md_escape(dt_line))
            # booking created text is plain, we send as HTML with no tags inside, so safe
            await send_client(chat_id, msg, reply_markup=main_menu(), parse_mode="HTML")
            mark_sent(record_id, "created", {"chat_id": chat_id, "phone": phone})
            await notify_admin(f"<b>✅ Отбивка о записи отправлена</b><br/>chat_id: <code>{chat_id}</code><br/>record_id: <code>{escape_html(record_id)}</code>")

        # schedule reminders (future only)
        if dt:
            delete_reminders_for_record(record_id)
            schedule_all_reminders(record_id, chat_id, dt, service)

    elif status == "update":
        # detect reschedule: datetime changed
        if dt and prev_dt and dt.isoformat() != prev_dt.isoformat():
            old_d, old_t = human_dt(prev_dt)
            new_d, new_t = human_dt(dt)
            if not was_sent(record_id, f"resched:{dt.isoformat()}"):
                await send_client(chat_id, tpl_rescheduled(old_d, old_t, new_d, new_t, service), reply_markup=main_menu(), parse_mode="HTML")
                mark_sent(record_id, f"resched:{dt.isoformat()}", {"chat_id": chat_id})
                await notify_admin(f"<b>🔁 Перенос отправлен</b><br/>chat_id: <code>{chat_id}</code><br/>record_id: <code>{escape_html(record_id)}</code>")
        # refresh reminders on any update with dt
        if dt:
            delete_reminders_for_record(record_id)
            schedule_all_reminders(record_id, chat_id, dt, service)

    elif status == "delete":
        # cancellation
        if dt:
            d_h, t_h = human_dt(dt)
        else:
            d_h, t_h = ("", "")
        if not was_sent(record_id, "cancelled"):
            await send_client(chat_id, tpl_cancelled(d_h or "—", t_h or "—", service), reply_markup=main_menu(), parse_mode="HTML")
            mark_sent(record_id, "cancelled", {"chat_id": chat_id})
            await notify_admin(f"<b>❌ Отбивка об отмене отправлена</b><br/>chat_id: <code>{chat_id}</code><br/>record_id: <code>{escape_html(record_id)}</code>")
        delete_reminders_for_record(record_id)

    return {"ok": True}

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    update = await request.json()

    # callback buttons
    if "callback_query" in update:
        cq = update["callback_query"]
        chat_id = cq.get("message", {}).get("chat", {}).get("id")
        data = cq.get("data", "")
        await tg_post("answerCallbackQuery", {"callback_query_id": cq.get("id")})

        if data == "menu:to_admin":
            st = get_state(chat_id)
            set_state(chat_id, "chat_to_admin", st.get("data", {}))
            await send_client(chat_id, "Напишите сообщение — я перешлю администратору.", reply_markup=main_menu(), parse_mode="HTML")
            return {"ok": True}

        if data == "menu:link_phone":
            st = get_state(chat_id)
            set_state(chat_id, "await_contact", st.get("data", {}))
            await send_client(
                chat_id,
                "Нажмите кнопку ниже, чтобы отправить номер телефона (нужно для напоминаний о записи).",
                reply_markup={"keyboard": [[{"text": "📱 Отправить номер", "request_contact": True}]],
                              "resize_keyboard": True, "one_time_keyboard": True},
                parse_mode="HTML",
            )
            return {"ok": True}

        return {"ok": True}

    message = update.get("message")
    if not message:
        return {"ok": True}

    chat_id = int(message["chat"]["id"])
    text = (message.get("text") or "").strip()
    contact = message.get("contact")

    # /chatid helper
    if text.startswith("/chatid"):
        await send_client(chat_id, f"chat_id: <code>{chat_id}</code>", parse_mode="HTML")
        await notify_admin(f"<b>CHATID</b><br/>chat_id: <code>{chat_id}</code>")
        return {"ok": True}

    # contact received
    if contact:
        phone = normalize_phone(contact.get("phone_number", "")) or contact.get("phone_number", "")
        set_phone_map(phone, chat_id)
        st = get_state(chat_id)
        set_state(chat_id, "idle", st.get("data", {}))
        await notify_admin(f"<b>📱 Привязка номера</b><br/>chat_id: <code>{chat_id}</code><br/>тел: <code>{escape_html(phone)}</code>")
        await send_client(chat_id, "Спасибо! Номер сохранён.", reply_markup=main_menu(), parse_mode="HTML")
        return {"ok": True}

    # greeting
    if text.lower() in ("/start", "start", "привет", "здравствуйте", "добрый день", "добрый вечер"):
        await send_client(chat_id, WELCOME_TEXT, reply_markup=main_menu(), parse_mode="HTML")
        return {"ok": True}

    # phone as text
    ph = normalize_phone(text)
    if ph:
        set_phone_map(ph, chat_id)
        await notify_admin(f"<b>📱 Привязка номера (текст)</b><br/>chat_id: <code>{chat_id}</code><br/>тел: <code>{escape_html(ph)}</code>")
        await send_client(chat_id, "Спасибо! Номер сохранён.", reply_markup=main_menu(), parse_mode="HTML")
        return {"ok": True}

    # confirmation / cancel markers
    if text in ("+", "＋"):
        await send_client(chat_id, "Спасибо! Визит подтверждён ✅", reply_markup=main_menu(), parse_mode="HTML")
        await notify_admin(f"<b>✅ Подтверждение</b><br/>chat_id: <code>{chat_id}</code>")
        return {"ok": True}

    if text in ("-", "−", "–"):
        await send_client(chat_id, "Поняла. Передала администратору запрос на отмену/перенос записи. 💬", reply_markup=main_menu(), parse_mode="HTML")
        await notify_admin(f"<b>⚠️ Запрос отмены/переноса</b><br/>chat_id: <code>{chat_id}</code>")
        return {"ok": True}

    # chat to admin mode
    st = get_state(chat_id)
    if st.get("step") == "chat_to_admin":
        await notify_admin(f"<b>📩 Входящее от клиента</b><br/>chat_id: <code>{chat_id}</code><br/>{escape_html(text)[:3500]}")
        await send_client(chat_id, "Сообщение передано администратору. Ответим вам в этом чате.", reply_markup=main_menu(), parse_mode="HTML")
        set_state(chat_id, "idle", st.get("data", {}))
        return {"ok": True}

    # default
    await send_client(
        chat_id,
        "Принято.\n\nЧтобы записаться — нажмите «Онлайн-запись».\nЕсли нужен администратор — нажмите «Написать администратору».",
        reply_markup=main_menu(),
        parse_mode="HTML",
    )
    return {"ok": True}
