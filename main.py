
import os
import json
import sqlite3
import re
from datetime import datetime
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Request, BackgroundTasks
import httpx
import openai

# ====== Переменные окружения (Environment variables) ======
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
YCLIENTS_USER_TOKEN = os.getenv("YCLIENTS_USER_TOKEN")
YCLIENTS_PARTNER_TOKEN = os.getenv("YCLIENTS_PARTNER_TOKEN")
YCLIENTS_COMPANY_ID = os.getenv("YCLIENTS_COMPANY_ID")
YCLIENTS_API_BASE = os.getenv("YCLIENTS_API_BASE", "https://api.yclients.com")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
BASE_URL = os.getenv("BASE_URL")

if OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY

app = FastAPI(title="KUTIKULA Bot")

# ====== Локальное хранилище состояния / журнал ======
DB_PATH = "user_state.db"
LOGS_PATH = "dialog_memory.json"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_state (
            chat_id INTEGER PRIMARY KEY,
            state TEXT,
            data TEXT,
            updated_at TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

def get_user_state(chat_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT state, data FROM user_state WHERE chat_id = ?", (chat_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None, {}
    state, data_json = row
    data = json.loads(data_json) if data_json else {}
    return state, data

def set_user_state(chat_id: int, state: str, data: dict):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    cur.execute("""
        INSERT INTO user_state(chat_id, state, data, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET state=excluded.state, data=excluded.data, updated_at=excluded.updated_at
    """, (chat_id, state, json.dumps(data, ensure_ascii=False), now))
    conn.commit()
    conn.close()

def clear_user_state(chat_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM user_state WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()

def append_log(chat_id: int, user_text: str, bot_text: str, meta: dict = None):
    meta = meta or {}
    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "chat_id": chat_id,
        "user_text": user_text,
        "bot_text": bot_text,
        "meta": meta
    }
    logs = []
    if os.path.exists(LOGS_PATH):
        try:
            with open(LOGS_PATH, "r", encoding="utf-8") as f:
                logs = json.load(f)
        except Exception:
            logs = []
    logs.append(entry)
    with open(LOGS_PATH, "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)

# ====== Утилиты для клавиатур Telegram ======
def make_reply_keyboard(rows: list, resize: bool = True, one_time: bool = False):
    return {"keyboard": rows, "resize_keyboard": resize, "one_time_keyboard": one_time}

def make_service_keyboard(labels: List[str], per_row: int = 1):
    rows = []
    row = []
    for i, lbl in enumerate(labels, start=1):
        row.append(f"{i}. {lbl}")
        if len(row) >= per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(["Отмена"])
    return make_reply_keyboard(rows)

def make_master_keyboard(masters: List[Dict[str,Any]]):
    rows = [[m.get("name", "—")] for m in masters]
    rows.append(["Любой", "Отмена"])
    return make_reply_keyboard(rows)

def make_slots_keyboard(formatted_slots: List[str]):
    rows = [[f"{i+1}. {s}"] for i, s in enumerate(formatted_slots, start=1)]
    rows.append(["Другие", "Отмена"])
    return make_reply_keyboard(rows)

# ====== Парсер простых правил и OpenAI-парсер ======
PHONE_RE = re.compile(r"(?:\+7|7|8)\s*\(?\d{3}\)?[\s-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}")

def local_rule_parser(text: str) -> dict:
    low = (text or "").lower()
    res = {"intent": None, "service": None, "master_pref": None, "date_hint": None, "name": None, "phone": None}
    ph = PHONE_RE.search(text)
    if ph:
        res["phone"] = ph.group(0)
        res["intent"] = "book"
    kws = {
        "маникюр": ["маникюр", "ногти"],
        "педикюр": ["педикюр"],
        "лазерная эпиляция": ["лазер", "эпиляц"],
        "стрижка": ["стрижк", "парикмах"],
        "брови": ["брови", "ресниц", "лами"],
    }
    for svc,klist in kws.items():
        for k in klist:
            if k in low:
                res["service"] = svc
                res["intent"] = "book"
                break
    m = re.search(r"к (мастеру )?(?P<master>[А-ЯЁа-яёA-Za-z\-\s]+)", text, flags=re.IGNORECASE)
    if m:
        res["master_pref"] = m.group("master").strip()
        res["intent"] = "book"
    if "завтра" in low:
        res["date_hint"] = "завтра"; res["intent"] = "book"
    if "выходн" in low:
        res["date_hint"] = "выходные"; res["intent"] = "book"
    nm = re.search(r"(меня зовут|я\s+—|я\s+)(?P<name>[А-ЯЁа-яёA-Za-z\-\s]{2,40})", text, flags=re.IGNORECASE)
    if nm:
        res["name"] = nm.group("name").strip()
    return res

async def parse_with_openai(text: str) -> dict:
    if not OPENAI_API_KEY:
        return {}
    try:
        prompt = (
            "You are an assistant that extracts structured booking info from a Russian user message. "
            "Return JSON only with keys: intent (book/other), service, master_pref, date_hint, name, phone. "
            "If a field is missing set null.\n\n"
            f"Text: {text}\n"
        )
        resp = await openai.ChatCompletion.acreate(
            model="gpt-4o-mini",
            messages=[{"role":"user","content":prompt}],
            max_tokens=250,
            temperature=0.0
        )
        content = resp.choices[0].message.content
        try:
            parsed = json.loads(content)
            return parsed
        except Exception:
            return {}
    except Exception:
        return {}

async def parse_user_text(text: str) -> dict:
    parsed = local_rule_parser(text)
    # supplement with AI when missing important fields
    if OPENAI_API_KEY and (not parsed.get("service") or not parsed.get("date_hint")):
        ai = await parse_with_openai(text)
        if isinstance(ai, dict):
            for k,v in ai.items():
                if ai.get(k) and not parsed.get(k):
                    parsed[k] = ai.get(k)
    return parsed

# ====== YCLIENTS helpers ======
async def fetch_json(method: str, url: str, headers: dict = None, params: dict = None, json_body: dict = None, timeout:int=15):
    headers = headers or {}
    async with httpx.AsyncClient() as client:
        try:
            if method.upper() == "GET":
                r = await client.get(url, headers=headers, params=params, timeout=timeout)
            else:
                r = await client.post(url, headers=headers, json=json_body, params=params, timeout=timeout)
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, {"_text": r.text}
        except Exception as e:
            return None, {"error": str(e)}

async def get_services_from_yclients() -> List[Dict[str,Any]]:
    YCLIENTS_API_BASE_LOCAL = YCLIENTS_API_BASE.rstrip("/")
    # try a few common endpoints used by different YCLIENTS accounts
    candidates = [
        f"{YCLIENTS_API_BASE_LOCAL}/api/v1/company/{YCLIENTS_COMPANY_ID}/services/",
    ]
    headers = {
        "Accept": "application/vnd.api.v2+json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {YCLIENTS_USER_TOKEN or ''}",
        "Partner": f"{YCLIENTS_COMPANY_ID or ''}",
        "X-Partner-Token": f"{YCLIENTS_PARTNER_TOKEN or ''}",
    }
    for url in candidates:
        status, content = await fetch_json("GET", url, headers=headers, timeout=15)
        print("YCLIENTS TRY:", url, "STATUS:", status, flush=True)
        if status in (200,201) and content:
            items = content.get("data") if isinstance(content, dict) and content.get("data") else (content if isinstance(content, list) else [])
            services = []
            for it in items:
                sid = it.get("id") or it.get("service_id")
                title = it.get("title") or it.get("name") or it.get("service_name")
                price = it.get("price") or it.get("cost") or it.get("price_value")
                category = it.get("category") or it.get("section") or None
                if sid and title:
                    services.append({"id": sid, "title": title, "price": price, "category": category, "raw": it})
            if services:
                return services
    print("YCLIENTS error content:", json.dumps(content, ensure_ascii=False)[:500], flush=True)
    return []

async def query_yclients_slots(service_id: int, staff_id: Optional[int] = None, limit:int=3) -> List[Dict[str,Any]]:
    YCLIENTS_API_BASE_LOCAL = YCLIENTS_API_BASE.rstrip("/")
    url = f"{YCLIENTS_API_BASE_LOCAL}/api/v1/companies/{YCLIENTS_COMPANY_ID}/book_times"
    params = {"service_ids": service_id, "limit": limit}
    if staff_id:
        params["staff_ids"] = staff_id
    status, content = await fetch_json("GET", url, headers={
        "Authorization": f"Bearer {YCLIENTS_USER_TOKEN or ''}",
        "Partner": f"{YCLIENTS_COMPANY_ID or ''}",
        "X-Partner-Token": f"{YCLIENTS_PARTNER_TOKEN or ''}"
    }, params=params, timeout=15)
    if status in (200,201) and content:
        items = content.get("data") if isinstance(content, dict) and content.get("data") else (content if isinstance(content, list) else [])
        slots = []
        for it in items:
            dt = it.get("datetime") or it.get("date_time") or it.get("time") or it.get("start") or it.get("dt")
            if not dt:
                for v in it.values():
                    if isinstance(v, str) and len(v) > 8 and any(ch.isdigit() for ch in v):
                        dt = v
                        break
            if dt:
                slots.append({"dt": dt, "raw": it})
            if len(slots) >= limit:
                break
        return slots
    return []

async def find_client_by_phone(phone: str) -> Optional[Dict[str,Any]]:
    YCLIENTS_API_BASE_LOCAL = YCLIENTS_API_BASE.rstrip("/")
    url = f"{YCLIENTS_API_BASE_LOCAL}/api/v1/companies/{YCLIENTS_COMPANY_ID}/clients"
    status, content = await fetch_json("GET", url, headers={
        "Authorization": f"Bearer {YCLIENTS_USER_TOKEN or ''}",
        "Partner": f"{YCLIENTS_COMPANY_ID or ''}",
        "X-Partner-Token": f"{YCLIENTS_PARTNER_TOKEN or ''}"
    }, params={"phone": phone}, timeout=15)
    if status in (200,201) and content:
        items = content.get("data") if isinstance(content, dict) and content.get("data") else (content if isinstance(content, list) else [])
        if isinstance(items, list) and len(items) > 0:
            return items[0]
    return None

async def create_client_in_yclients(name: str, phone: str) -> Optional[Dict[str,Any]]:
    YCLIENTS_API_BASE_LOCAL = YCLIENTS_API_BASE.rstrip("/")
    url = f"{YCLIENTS_API_BASE_LOCAL}/api/v1/companies/{YCLIENTS_COMPANY_ID}/clients"
    payload = {"client": {"name": name, "phone": phone}}
    status, content = await fetch_json("POST", url, headers={
        "Authorization": f"Bearer {YCLIENTS_USER_TOKEN or ''}",
        "Partner": f"{YCLIENTS_COMPANY_ID or ''}",
        "X-Partner-Token": f"{YCLIENTS_PARTNER_TOKEN or ''}",
        "Content-Type": "application/json"
    }, json_body=payload, timeout=15)
    if status in (200,201,202):
        return content
    return None

async def create_booking_in_yclients(service_id: int, datetime_iso: str, client_id: Optional[int], client_name: str, client_phone: str, staff_id: Optional[int] = None):
    YCLIENTS_API_BASE_LOCAL = YCLIENTS_API_BASE.rstrip("/")
    url = f"{YCLIENTS_API_BASE_LOCAL}/api/v1/companies/{YCLIENTS_COMPANY_ID}/bookings"
    payload = {"client": {"id": client_id, "name": client_name, "phone": client_phone}, "service": {"id": service_id}, "datetime": datetime_iso}
    if staff_id:
        payload["staff_id"] = staff_id
    status, content = await fetch_json("POST", url, headers={
        "Authorization": f"Bearer {YCLIENTS_USER_TOKEN or ''}",
        "Partner": f"{YCLIENTS_COMPANY_ID or ''}",
        "X-Partner-Token": f"{YCLIENTS_PARTNER_TOKEN or ''}",
        "Content-Type": "application/json"
    }, json_body=payload, timeout=15)
    return status, content

# ====== Вспомогательные функции ======
def format_slot_display(dt_str: str) -> str:
    try:
        parsed = datetime.fromisoformat(dt_str)
        return parsed.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(dt_str)

# ====== Логика диалога ======
async def send_telegram_message(chat_id: int, text: str, parse_mode: Optional[str] = "HTML", reply_markup: Optional[dict] = None):
    if not TELEGRAM_TOKEN:
        print("TELEGRAM_TOKEN not set", flush=True)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(url, json=payload, timeout=15.0)
            if r.status_code != 200:
                print("sendMessage error:", r.status_code, r.text, flush=True)
        except Exception as e:
            print("sendMessage exception:", str(e), flush=True)

async def handle_user_message(chat_id: int, text: str, background_tasks: BackgroundTasks):
    text = (text or "").strip()
    state, data = get_user_state(chat_id)
    # Приветствие / старт
    if not state and text.lower() in ("", "/start", "привет", "записаться", "хочу запись", "start"):
        welcome = "Здравствуйте!🌸\nЯ — виртуальный администратор beauty studio KUTIKULA. Чем могу помочь?"
        kb = make_reply_keyboard([["Записаться", "Узнать цены"], ["Связаться с администратором"]], one_time=True)
        await send_telegram_message(chat_id, welcome, reply_markup=kb)
        append_log(chat_id, text, welcome)
        set_user_state(chat_id, "await_intent", {})
        return

    if state == "await_intent":
        parsed = await parse_user_text(text)
        if parsed.get("intent") == "book" or text.lower().startswith("запис"):
            services = await get_services_from_yclients()
            if not services:
                await send_telegram_message(chat_id, "Извините, не удалось загрузить список услуг. Попробуйте позже.")
                append_log(chat_id, text, "Не удалось загрузить список услуг")
                clear_user_state(chat_id)
                return
            labels = [f"{s['title']} — {s.get('price') or ''}" for s in services]
            kb = make_service_keyboard(labels, per_row=1)
            await send_telegram_message(chat_id, "Пожалуйста, выберите услугу:", reply_markup=kb)
            set_user_state(chat_id, "choose_service", {"services": services, "parsed": parsed})
            append_log(chat_id, text, "Показаны услуги", {"services_count": len(services)})
            return
        else:
            await send_telegram_message(chat_id, "Я могу помочь с записью. Напишите 'Записаться' или опишите желаемую услугу.")
            append_log(chat_id, text, "Попросили разъяснить")
            return

    if state == "choose_service":
        services = data.get("services", [])
        idx = None
        try:
            if text.strip().split(".")[0].isdigit():
                idx = int(text.strip().split(".")[0]) - 1
            else:
                for i,s in enumerate(services):
                    if s["title"].lower() in text.lower():
                        idx = i; break
        except Exception:
            idx = None
        if idx is None or idx < 0 or idx >= len(services):
            await send_telegram_message(chat_id, "Пожалуйста, выберите услугу корректно (нажмите кнопку с номером).")
            append_log(chat_id, text, "Неправильный выбор услуги")
            return
        chosen = services[idx]
        data["chosen_service"] = chosen
        # получаем список мастеров (если есть)
        masters = []
        raw = chosen.get("raw") or {}
        if isinstance(raw, dict) and raw.get("staffs"):
            for st in raw.get("staffs"):
                masters.append({"id": st.get("id"), "name": st.get("name")})
        if not masters:
            st_url = f"{YCLIENTS_API_BASE.rstrip('/')}/api/v1/companies/{YCLIENTS_COMPANY_ID}/staffs"
            status, content = await fetch_json("GET", st_url, headers={
                "Authorization": f"Bearer {YCLIENTS_USER_TOKEN or ''}",
                "Partner": f"{YCLIENTS_COMPANY_ID or ''}",
                "X-Partner-Token": f"{YCLIENTS_PARTNER_TOKEN or ''}"
            })
            if status in (200,201) and content:
                items = content.get("data") if isinstance(content, dict) and content.get("data") else (content if isinstance(content, list) else [])
                for it in items:
                    masters.append({"id": it.get("id"), "name": it.get("name")})
        data["masters"] = masters
        set_user_state(chat_id, "choose_master", data)
        if masters:
            kb = make_master_keyboard(masters)
            await send_telegram_message(chat_id, f"К какому мастеру хотите записаться?", reply_markup=kb)
            append_log(chat_id, text, "Показаны мастера", {"masters_count": len(masters)})
            return
        else:
            set_user_state(chat_id, "enter_name", data)
            await send_telegram_message(chat_id, "Пожалуйста, укажите ваше имя и фамилию:")
            append_log(chat_id, text, "Мастера не найдены, запрос имени")
            return

    if state == "choose_master":
        masters = data.get("masters", [])
        if text.lower() in ("любой","any"):
            data["chosen_master"] = None
        else:
            sel = None
            try:
                if text.strip().split('.')[0].isdigit():
                    sel = int(text.strip().split('.')[0]) - 1
                else:
                    for i,m in enumerate(masters):
                        if m.get('name','').lower() in text.lower():
                            sel = i; break
            except Exception:
                sel = None
        if sel is None and text.lower() not in ("любой","any"):
            await send_telegram_message(chat_id, "Пожалуйста, выберите мастера из списка или напишите 'Любой'.")
            append_log(chat_id, text, "Неправильный выбор мастера")
            return
        if text.lower() in ("любой","any"):
            data["chosen_master"] = None
        else:
            data["chosen_master"] = masters[sel]
        set_user_state(chat_id, "enter_name", data)
        await send_telegram_message(chat_id, "Пожалуйста, укажите ваше имя и фамилию:")
        append_log(chat_id, text, "Выбран мастер", {"chosen_master": data.get("chosen_master")})
        return

    if state == "enter_name":
        data["client_name"] = text.strip()
        set_user_state(chat_id, "enter_phone", data)
        await send_telegram_message(chat_id, "Спасибо. Пожалуйста, укажите номер телефона в формате +7XXXXXXXXXX:")
        append_log(chat_id, text, "Введено имя", {"name": data.get("client_name")})
        return

    if state == "enter_phone":
        phone = text.strip()
        data["client_phone"] = phone
        set_user_state(chat_id, "search_slots", data)
        await send_telegram_message(chat_id, "Ищу ближайшие свободные окошки...")
        append_log(chat_id, text, "Введён телефон", {"phone": phone})
        client = await find_client_by_phone(phone)
        client_id = None
        if client and isinstance(client, dict):
            client_id = client.get("id") or client.get("client_id") or client.get("clientId")
        else:
            created = await create_client_in_yclients(data.get("client_name"), phone)
            if created and isinstance(created, dict):
                if created.get("data") and isinstance(created.get("data"), dict):
                    client_id = created.get("data").get("id")
                else:
                    client_id = created.get("id") or created.get("client_id")
        svc = data.get("chosen_service") or {}
        svc_id = svc.get("id") or (svc.get("raw") and svc.get("raw").get("id"))
        staff_id = data.get("chosen_master") and data.get("chosen_master").get("id") or None
        slots = await query_yclients_slots(svc_id, staff_id=staff_id, limit=3)
        if not slots:
            await send_telegram_message(chat_id, "К сожалению, свободных времён не найдено. Хотите расширить поиск? (Да/Нет)")
            set_user_state(chat_id, "ask_expand", data)
            return
        data["slots"] = slots
        data["client_id"] = client_id
        set_user_state(chat_id, "choose_slot", data)
        formatted = [format_slot_display(s.get("dt")) for s in slots]
        kb = make_slots_keyboard(formatted)
        await send_telegram_message(chat_id, f"Нашла ближайшие варианты:\n\n{chr(10).join(formatted)}\n\nВыберите номер:", reply_markup=kb)
        append_log(chat_id, text, "Показаны слоты", {"slots": formatted})
        return

    if state == "ask_expand":
        if text.lower() in ("да","yes"):
            data = data or {}
            svc = data.get("chosen_service") or {}
            svc_id = svc.get("id") or (svc.get("raw") and svc.get("raw").get("id"))
            staff_id = data.get("chosen_master") and data.get("chosen_master").get("id") or None
            slots = await query_yclients_slots(svc_id, staff_id=staff_id, limit=8)
            if not slots:
                await send_telegram_message(chat_id, "К сожалению, не найдено других вариантов.")
                clear_user_state(chat_id)
                return
            data["slots"] = slots
            set_user_state(chat_id, "choose_slot", data)
            formatted = [format_slot_display(s.get("dt")) for s in slots]
            kb = make_slots_keyboard(formatted)
            await send_telegram_message(chat_id, f"Найдены другие варианты:\n\n{chr(10).join(formatted)}", reply_markup=kb)
            return
        else:
            await send_telegram_message(chat_id, "Хорошо. Чтобы начать заново — напишите /start.")
            clear_user_state(chat_id)
            return

    if state == "choose_slot":
        slots = data.get("slots", [])
        try:
            idx = int(text.strip().split('.')[0]) - 1
            if idx < 0 or idx >= len(slots):
                raise ValueError()
            chosen = slots[idx]
            dt_raw = chosen.get("dt")
            try:
                dt_iso = datetime.fromisoformat(dt_raw).isoformat()
            except Exception:
                dt_iso = dt_raw
            data["chosen_slot"] = {"iso": dt_iso, "raw": chosen}
            set_user_state(chat_id, "confirm", data)
            await send_telegram_message(chat_id, f"Подтвердите запись: {data.get('client_name')} — {data.get('chosen_service',{}).get('title')} — {format_slot_display(dt_raw)}.\nНапишите 'Да' для подтверждения или 'Отмена' для отмены.")
            append_log(chat_id, text, "Выбран слот", {"chosen": format_slot_display(dt_raw)})
            return
        except Exception:
            await send_telegram_message(chat_id, "Введите корректный номер варианта.")
            return

    if state == "confirm":
        if text.lower() in ("да","yes","confirm"):
            data = data or {}
            svc_id = data.get("chosen_service",{}).get("id")
            dt_iso = data.get("chosen_slot",{}).get("iso")
            client_id = data.get("client_id")
            name = data.get("client_name")
            phone = data.get("client_phone")
            staff_id = data.get("chosen_master") and data.get("chosen_master").get("id") or None
            status, resp = await create_booking_in_yclients(svc_id, dt_iso, client_id, name, phone, staff_id=staff_id)
            if status in (200,201,202):
                await send_telegram_message(chat_id, f"✅ Запись создана: {data.get('chosen_service',{}).get('title')} — {format_slot_display(data.get('chosen_slot',{}).get('raw',{}).get('dt'))}. \nСпасибо, мы свяжемся для подтверждения.")
                append_log(chat_id, text, "Запись создана", {"response": resp})
                if ADMIN_CHAT_ID:
                    admin_msg = f"📌 Новая запись:\n{name} ({phone})\nУслуга: {data.get('chosen_service',{}).get('title')}\nВремя: {data.get('chosen_slot',{}).get('iso')}\nМастер: {data.get('chosen_master') and data.get('chosen_master').get('name')}"
                    background_tasks.add_task(send_telegram_message, int(ADMIN_CHAT_ID), admin_msg)
                clear_user_state(chat_id)
                return
            else:
                await send_telegram_message(chat_id, "❗️ Не удалось создать запись. Попробуйте позже или свяжитесь с администратором.")
                append_log(chat_id, text, "Ошибка создания записи", {"status": status, "resp": resp})
                clear_user_state(chat_id)
                return
        elif text.lower() in ("отмена","cancel","нет"):
            await send_telegram_message(chat_id, "Запись отменена. Чтобы начать заново — напишите /start.")
            clear_user_state(chat_id)
            return
        else:
            await send_telegram_message(chat_id, "Пожалуйста, подтвердите: напишите 'Да' или 'Отмена'.")
            return

    # fallback: try parse and start booking flow
    parsed = await parse_user_text(text)
    if parsed.get("intent") == "book":
        services = await get_services_from_yclients()
        if not services:
            await send_telegram_message(chat_id, "Извините, не удалось загрузить услуги.")
            return
        sel_idx = None
        if parsed.get("service"):
            for i,s in enumerate(services):
                if parsed.get("service").lower() in s.get("title","").lower():
                    sel_idx = i; break
        if sel_idx is None:
            labels = [f"{s['title']} — {s.get('price') or ''}" for s in services]
            kb = make_service_keyboard(labels, per_row=1)
            await send_telegram_message(chat_id, "Пожалуйста, выберите услугу:", reply_markup=kb)
            set_user_state(chat_id, "choose_service", {"services": services, "parsed": parsed})
            append_log(chat_id, text, "Показаны услуги (fallback)")
            return
        chosen = services[sel_idx]
        data = {"chosen_service": chosen, "parsed": parsed}
        set_user_state(chat_id, "enter_name", data)
        await send_telegram_message(chat_id, "Хорошо. Пожалуйста, укажите имя и фамилию для записи:")
        append_log(chat_id, text, "Авто-перенаправление: запрос имени", {"service": chosen.get("title")})
        return

    await send_telegram_message(chat_id, "Я не совсем поняла. Напишите 'Записаться' или опишите, какую услугу хотите.")
    append_log(chat_id, text, "Не распознано")
    return

# ====== Endpoints для FastAPI ======
@app.post('/telegram-webhook')
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    update = await request.json()
    message = update.get('message') or update.get('edited_message') or (update.get('callback_query') or {}).get('message')
    if not message:
        return {'ok': True}
    chat = message.get('chat', {})
    chat_id = chat.get('id')
    text = message.get('text') or message.get('caption') or ''
    await handle_user_message(chat_id, text, background_tasks)
    return {'ok': True}

@app.post('/yclients-webhook')
async def yclients_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        payload = await request.json()
    except Exception:
        payload = {'raw': await request.body()}
    pretty = json.dumps(payload, ensure_ascii=False, indent=2)
    print('YCLIENTS webhook:', pretty, flush=True)
    if ADMIN_CHAT_ID:
        background_tasks.add_task(send_telegram_message, int(ADMIN_CHAT_ID), f"Событие YCLIENTS:\n<pre>{pretty}</pre>", 'HTML')
    return {'status':'ok'}

@app.get('/')
async def root():
    return {'status':'ok'}
