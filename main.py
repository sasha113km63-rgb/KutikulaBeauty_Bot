# main.py
import os
import json
import time
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime

import httpx
import openai

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse

# --- logging ----------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kutikula_bot")

# --- env vars ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
YCLIENTS_USER_TOKEN = os.getenv("YCLIENTS_USER_TOKEN")
YCLIENTS_PARTNER_TOKEN = os.getenv("YCLIENTS_PARTNER_TOKEN")  # optional
YCLIENTS_COMPANY_ID = os.getenv("YCLIENTS_COMPANY_ID")  # required
YCLIENTS_API_BASE = os.getenv("YCLIENTS_API_BASE", "https://api.yclients.com")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
BASE_URL = os.getenv("BASE_URL")  # e.g. https://your-service.onrender.com

if OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY

if not TELEGRAM_TOKEN:
    logger.warning("TELEGRAM_TOKEN not set! Telegram features will not work.")

if not YCLIENTS_COMPANY_ID:
    logger.warning("YCLIENTS_COMPANY_ID not set! YClients company ID required for requests.")

# --- storage files ----------------
DIALOGS_FILE = "dialog_memory.json"

# ensure file exists
if not os.path.exists(DIALOGS_FILE):
    with open(DIALOGS_FILE, "w", encoding="utf-8") as f:
        json.dump({}, f, ensure_ascii=False, indent=2)

# --- helpers ----------------
def load_dialogs() -> Dict[str, Any]:
    try:
        with open(DIALOGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_dialogs(data: Dict[str, Any]):
    with open(DIALOGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def append_dialog(chat_id: str, entry: Dict[str, Any]):
    data = load_dialogs()
    data.setdefault(chat_id, []).append(entry)
    save_dialogs(data)

async def call_openai_parse(user_text: str) -> Dict[str, Any]:
    """
    Простая обёртка для разбора запроса через OpenAI.
    Возвращает dict с полями: intent, requested_service, date, time, raw
    (это облегчённая обработка — можно расширить)
    """
    if not OPENAI_API_KEY:
        return {"intent": None, "requested_service": None, "date": None, "time": None, "raw": user_text}

    prompt = (
        f"Пользователь написал: \"{user_text}\"\n"
        "Определи намерение (intent): запись или просто вопрос. Если запись — попробуй "
        "выделить название услуги (service), желаемую дату (date) и время (time). "
        "Если не уверенно — оставь null для поля.\n\n"
        "Верни JSON с полями: intent, service, date, time."
    )
    try:
        resp = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":prompt}],
            max_tokens=200,
        )
        txt = resp["choices"][0]["message"]["content"].strip()
        # ожидаем JSON — попробуем распарсить
        try:
            parsed = json.loads(txt)
            return {
                "intent": parsed.get("intent"),
                "requested_service": parsed.get("service"),
                "date": parsed.get("date"),
                "time": parsed.get("time"),
                "raw": user_text,
            }
        except Exception:
            # если OpenAI ответил в свободной форме — вернём минимально полезную структуру
            return {"intent": None, "requested_service": None, "date": None, "time": None, "raw": user_text}
    except Exception as e:
        logger.exception("OpenAI call failed")
        return {"intent": None, "requested_service": None, "date": None, "time": None, "raw": user_text}

# --- YCLIENTS helpers ----------------
async def try_yclients_get_services() -> (int, Any):
    """
    Попробуем несколько популярных вариантов endpoint'ов и заголовков чтобы получить список услуг.
    Вернём (status_code, data) — data может быть dict/list либо текст ошибки.
    """
    base = YCLIENTS_API_BASE.rstrip("/")
    # candidate endpoints (проверяем несколько вариантов)
    endpoints = [
        f"{base}/api/v1/company/{YCLIENTS_COMPANY_ID}/services",
        f"{base}/api/v1/companies/{YCLIENTS_COMPANY_ID}/services",
        f"{base}/api/v1/services?company_id={YCLIENTS_COMPANY_ID}",
        f"{base}/api/v1/companies/services?company_id={YCLIENTS_COMPANY_ID}",
    ]
    # header variants
    header_variants = []

    # Variant A: Bearer user token (обычно хватает)
    if YCLIENTS_USER_TOKEN:
        header_variants.append({
            "Authorization": f"Bearer {YCLIENTS_USER_TOKEN}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    # Variant B: X-Partner-Token + Partner-Id / Partner
    if YCLIENTS_PARTNER_TOKEN:
        header_variants.append({
            "X-Partner-Token": YCLIENTS_PARTNER_TOKEN,
            "Partner-Id": YCLIENTS_COMPANY_ID,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        header_variants.append({
            "X-Partner-Token": YCLIENTS_PARTNER_TOKEN,
            "Partner": YCLIENTS_COMPANY_ID,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    # Variant C: both Bearer and partner headers
    if YCLIENTS_USER_TOKEN and YCLIENTS_PARTNER_TOKEN:
        header_variants.append({
            "Authorization": f"Bearer {YCLIENTS_USER_TOKEN}",
            "X-Partner-Token": YCLIENTS_PARTNER_TOKEN,
            "Partner-Id": YCLIENTS_COMPANY_ID,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    async with httpx.AsyncClient(timeout=20.0) as client:
        for url in endpoints:
            for headers in header_variants:
                try:
                    logger.info("YCLIENTS TRY (%s) HEADERS: %s", url, {k: (v[:6] + "...") if "Token" in k or "Authorization" in k else v for k,v in headers.items()})
                    r = await client.get(url, headers=headers)
                    status = r.status_code
                    # log content for debugging
                    logger.info("YCLIENTS RESPONSE (%s) STATUS: %s CONTENT: %s", headers.get("Authorization") or headers.get("X-Partner-Token","-"), status, r.text[:300])
                    if status == 200:
                        try:
                            return status, r.json()
                        except Exception:
                            return status, r.text
                    # continue trying other combos
                except Exception as e:
                    logger.exception("Error while trying services endpoint")
                    continue
    # если ничего не сработало:
    return 500, {"error": "all endpoints tried and failed"}

async def try_yclients_create_booking(payload: Dict[str, Any]) -> (int, Any):
    """
    Попытка создать запись в YCLIENTS. Возвращает (status, response).
    ВНИМАНИЕ: конкретный эндпойнт и формат payload различается для разных инсталляций YCLIENTS.
    Тут делаем разумные попытки, и если всё упадёт — вернём ошибку и админ получит уведомление.
    """
    base = YCLIENTS_API_BASE.rstrip("/")
    # возможные endpoint'ы для создания записи/appointments
    booking_endpoints = [
        f"{base}/api/v1/companies/{YCLIENTS_COMPANY_ID}/appointments",
        f"{base}/api/v1/company/{YCLIENTS_COMPANY_ID}/appointments",
        f"{base}/api/v1/appointment",
        f"{base}/api/v1/companies/{YCLIENTS_COMPANY_ID}/create_appointment",
    ]
    # headers (используем схему Authorization + возможно партнер)
    header_variants = []
    if YCLIENTS_USER_TOKEN:
        header_variants.append({
            "Authorization": f"Bearer {YCLIENTS_USER_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
    if YCLIENTS_PARTNER_TOKEN:
        header_variants.append({
            "X-Partner-Token": YCLIENTS_PARTNER_TOKEN,
            "Partner-Id": YCLIENTS_COMPANY_ID,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
    if YCLIENTS_USER_TOKEN and YCLIENTS_PARTNER_TOKEN:
        header_variants.append({
            "Authorization": f"Bearer {YCLIENTS_USER_TOKEN}",
            "X-Partner-Token": YCLIENTS_PARTNER_TOKEN,
            "Partner-Id": YCLIENTS_COMPANY_ID,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    async with httpx.AsyncClient(timeout=20.0) as client:
        for url in booking_endpoints:
            for headers in header_variants:
                try:
                    logger.info("YCLIENTS BOOKING TRY %s HEADERS: %s", url, {k: (v[:6]+"...") if "Token" in k or "Authorization" in k else v for k,v in headers.items()})
                    r = await client.post(url, json=payload, headers=headers)
                    logger.info("YCLIENTS BOOKING RESPONSE: %s %s", r.status_code, r.text[:300])
                    if r.status_code in (200, 201):
                        try:
                            return r.status_code, r.json()
                        except Exception:
                            return r.status_code, r.text
                    # если 4xx/5xx - пробуем дальше
                except Exception:
                    logger.exception("error creating booking attempt")
                    continue
    return 500, {"error": "all booking endpoints tried and failed"}

# --- Telegram helpers ----------------
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}" if TELEGRAM_TOKEN else None

async def telegram_send_message(chat_id: str, text: str, parse_mode: str = "HTML"):
    if not TELEGRAM_API_BASE:
        logger.warning("No TELEGRAM_TOKEN, skipping send_message")
        return
    url = f"{TELEGRAM_API_BASE}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode})

async def telegram_set_webhook():
    """
    Вызвать эту функцию вручную (или закомментировать вызов), если хотите
    автоматически регистрировать webhook при старте.
    """
    if not TELEGRAM_API_BASE or not BASE_URL:
        logger.warning("Can't set webhook: TELEGRAM_TOKEN or BASE_URL missing")
        return
    webhook_url = BASE_URL.rstrip("/") + "/telegram-webhook"
    url = f"{TELEGRAM_API_BASE}/setWebhook"
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json={"url": webhook_url})
        logger.info("setWebhook response: %s %s", r.status_code, r.text)

# --- Bot logic state machine in memory (also persisted) ----------
# We'll keep minimal per-chat state: stage, chosen_service_id, chosen_service_name, date, time, name, phone
IN_MEMORY_STATE: Dict[str, Dict[str, Any]] = {}
STATE_SAVE_INTERVAL = 10  # not used heavily; state persisted into dialog logs as needed

def start_booking_flow(chat_id: str):
    IN_MEMORY_STATE[chat_id] = {
        "stage": "choose_service",
        "service": None,
        "date": None,
        "time": None,
        "name": None,
        "phone": None,
        "created_at": datetime.utcnow().isoformat()
    }

# --- FastAPI app ----------------
app = FastAPI(title="KUTIKULA Bot")

@app.on_event("startup")
async def startup_event():
    logger.info("Application startup complete.")
    # optionally set webhook on startup — enable if you want auto registration
    # await telegram_set_webhook()

# Telegram webhook endpoint
@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        print("📩 Incoming Telegram update:", json.dumps(data, ensure_ascii=False, indent=2))

        # Проверим, есть ли сообщение
        if "message" in data:
            chat_id = data["message"]["chat"]["id"]
            text = data["message"].get("text", "")

            # Простейший ответ пользователю
            await send_message(chat_id, f"Вы написали: {text}")

            # Отправка уведомления администратору (если включено)
            if ADMIN_CHAT_ID and str(chat_id) != str(ADMIN_CHAT_ID):
                await send_message(ADMIN_CHAT_ID, f"📨 Новое сообщение от пользователя {chat_id}: {text}")

        return {"ok": True}

    except Exception as e:
        print("❌ Ошибка в webhook:", e)
        return {"ok": False, "error": str(e)}

async def process_user_message(chat_id: str, text: str):
    """
    Основная обработка входящих сообщений.
    """
    state = IN_MEMORY_STATE.get(chat_id)
    # simple commands
    if text.startswith("/start"):
        await telegram_send_message(chat_id, "Привет! Я бот записи. Отправь /services чтобы увидеть список услуг или просто напиши 'записаться'.")
        append_dialog(chat_id, {"from":"bot","text":"greeting","ts":time.time()})
        return
    if text.startswith("/services"):
        # get services
        status, data = await try_yclients_get_services()
        if status == 200:
            # Expect data structure - adapt to received format
            services_list = []
            # try to parse common formats:
            if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
                services_list = data["data"]
            elif isinstance(data, list):
                services_list = data
            else:
                # fallback: try to extract list elements
                services_list = data if isinstance(data, list) else []
            if not services_list:
                await telegram_send_message(chat_id, "Список услуг пуст или API вернул неожиданный формат.")
                return
            # Compose message
            msg_lines = ["Список услуг:"]
            for s in services_list[:50]:
                # try common fields
                sid = s.get("id") or s.get("service_id") or s.get("serviceId") or s.get("serviceID")
                name = s.get("name") or s.get("title") or s.get("service")
                price = None
                # try price fields
                if "price" in s and s["price"]:
                    price = s["price"]
                elif s.get("default_price"):
                    price = s.get("default_price")
                elif isinstance(s.get("prices"), list) and s.get("prices"):
                    price = s["prices"][0].get("price")
                line = f"- {name} (id: {sid})" + (f" — {price}₽" if price is not None else "")
                msg_lines.append(line)
            await telegram_send_message(chat_id, "\n".join(msg_lines))
            append_dialog(chat_id, {"from":"bot","text":"listed_services","ts":time.time()})
        else:
            await telegram_send_message(chat_id, f"Не удалось получить список услуг: {data}")
        return

    # If user is in booking flow
    if state:
        stage = state["stage"]
        if stage == "choose_service":
            # user should provide service id or name; allow them to type id
            # try to interpret text as id:
            chosen = text.strip()
            state["service"] = chosen
            state["stage"] = "ask_date"
            IN_MEMORY_STATE[chat_id] = state
            await telegram_send_message(chat_id, "Выбранная услуга: %s\nУкажите желаемую дату (например 2025-10-20 или 'завтра')" % chosen)
            append_dialog(chat_id, {"from":"bot","text":"ask_date","ts":time.time()})
            return
        elif stage == "ask_date":
            # attempt simple parse or accept raw
            state["date"] = text.strip()
            state["stage"] = "ask_time"
            IN_MEMORY_STATE[chat_id] = state
            await telegram_send_message(chat_id, "Укажите время (например 15:30)")
            return
        elif stage == "ask_time":
            state["time"] = text.strip()
            state["stage"] = "ask_name"
            IN_MEMORY_STATE[chat_id] = state
            await telegram_send_message(chat_id, "Как вас зовут?")
            return
        elif stage == "ask_name":
            state["name"] = text.strip()
            state["stage"] = "ask_phone"
            IN_MEMORY_STATE[chat_id] = state
            await telegram_send_message(chat_id, "Телефон для контакта (можно в любом формате)")
            return
        elif stage == "ask_phone":
            state["phone"] = text.strip()
            state["stage"] = "confirm"
            IN_MEMORY_STATE[chat_id] = state
            # show confirmation
            confirm_text = (
                f"Подтвердите запись:\n"
                f"Услуга: {state.get('service')}\n"
                f"Дата: {state.get('date')} {state.get('time')}\n"
                f"Клиент: {state.get('name')}\n"
                f"Телефон: {state.get('phone')}\n\n"
                "Напишите 'да' для подтверждения, 'отмена' для отмены."
            )
            await telegram_send_message(chat_id, confirm_text)
            return
        elif stage == "confirm":
            if text.lower() in ("да", "ok", "подтвердить", "да, подтверждаю"):
                # Attempt booking
                payload = {
                    # NOTE: payload keys depend on YCLIENTS API — этот пример может требовать правки под ваш аккаунт.
                    "company_id": int(YCLIENTS_COMPANY_ID) if YCLIENTS_COMPANY_ID else None,
                    "service": state.get("service"),
                    "date": state.get("date"),
                    "time": state.get("time"),
                    "client_name": state.get("name"),
                    "client_phone": state.get("phone"),
                    "notes": "Created via Telegram bot"
                }
                append_dialog(chat_id, {"from":"bot","text":"creating_booking","payload":payload,"ts":time.time()})
                status, resp = await try_yclients_create_booking(payload)
                if status in (200, 201):
                    await telegram_send_message(chat_id, "Запись успешно создана в YCLIENTS.")
                    append_dialog(chat_id, {"from":"bot","text":"booking_created","resp":resp,"ts":time.time()})
                    # notify admin
                    if ADMIN_CHAT_ID:
                        await telegram_send_message(ADMIN_CHAT_ID, f"Новая запись создана: {json.dumps(payload, ensure_ascii=False)}\nОтвет YCLIENTS: {json.dumps(resp, ensure_ascii=False)}")
                else:
                    # failed to create — notify admin with full details and inform user
                    await telegram_send_message(chat_id, "Не удалось автоматически создать запись в YCLIENTS. Администратор получит уведомление и свяжется с вами.")
                    if ADMIN_CHAT_ID:
                        await telegram_send_message(ADMIN_CHAT_ID, f"Ошибка создания записи. Детали: {json.dumps(payload, ensure_ascii=False)}. Результат попыток: {json.dumps(resp, ensure_ascii=False)}")
                    append_dialog(chat_id, {"from":"bot","text":"booking_failed","resp":resp,"ts":time.time()})
                # clear state
                IN_MEMORY_STATE.pop(chat_id, None)
                return
            else:
                # cancel or other
                IN_MEMORY_STATE.pop(chat_id, None)
                await telegram_send_message(chat_id, "Запись отменена.")
                return

    # If not in state and message looks like booking request — use OpenAI parse to detect
    parsed = await call_openai_parse(text)
    if parsed.get("intent") and parsed["intent"].lower().startswith("book"):
        # start flow
        start_booking_flow(chat_id)
        IN_MEMORY_STATE[chat_id]["requested_service_from_nlp"] = parsed.get("requested_service")
        # prefill if present
        if parsed.get("requested_service"):
            IN_MEMORY_STATE[chat_id]["service"] = parsed.get("requested_service")
            IN_MEMORY_STATE[chat_id]["stage"] = "ask_date"
            await telegram_send_message(chat_id, f"Понял — вы хотите услугу: {parsed.get('requested_service')}. Укажите желаемую дату.")
        else:
            await telegram_send_message(chat_id, "Начинаем запись. Укажите ID или название услуги (можно /services чтобы увидеть список).")
        return

    # default fallback
    await telegram_send_message(chat_id, "Не понял запрос. Напишите /services чтобы увидеть список услуг или 'записаться' для создания записи.")
    return

# --- Admin & debug endpoints ---
@app.get("/_health")
async def health():
    return {"status": "ok"}

@app.get("/dump-dialogs")
async def dump_dialogs():
    return load_dialogs()

# --- Notes for deployment and usage -----------
"""
Пояснения и рекомендации:
1) Переменные окружения (Render) — ОБЯЗАТЕЛЬНО заполните:
   - TELEGRAM_TOKEN
   - BASE_URL
   - YCLIENTS_USER_TOKEN
   - YCLIENTS_COMPANY_ID
   - ADMIN_CHAT_ID (рекомендовано)
   - OPENAI_API_KEY (опционально)

2) Webhook: на Render в настройках можно установить публичный URL сервиса. После deploy:
   - либо раскомментируйте вызов await telegram_set_webhook() в startup,
   - либо вручную установить webhook через: https://api.telegram.org/bot<token>/setWebhook?url=<BASE_URL>/telegram-webhook

3) YCLIENTS API: если ваш аккаунт требует специальных прав — проверьте в кабинете разработчика (Permissions) и убедитесь, что при подключении интеграции вы назначили системному пользователю права на чтение услуг и создание записей.

4) Если создание записи через API не работает (401 с сообщением про партнёра) — проверьте:
   - в карточке приложения YCLIENTS: какие есть Partner token и Partner ID; добавьте их в переменные окружения,
   - в интерфейсе филиала: доступы (Permissions) для интеграции (должна быть включена выдача User token и права на запись).

5) Логика создания записи здесь осторожная: она пробует несколько endpoint'ов и заголовков. Если все варианты не сработают — бот отправит полную информацию в ADMIN_CHAT_ID, чтобы админ мог вручную создать запись.

6) Отладка: смотрите логи Render — код логирует попытки и входящие ответы YCLIENTS (первые ~300 символов), чтобы вы могли понять, какие заголовки подходят.

7) Тонкости: если вы точно знаете конечную точку и формат JSON для создания appointment в вашей версии YCLIENTS API — скажите мне, и я подстрою `try_yclients_create_booking` под точный payload (тогда создание будет автоматическим).
"""

# End of file
