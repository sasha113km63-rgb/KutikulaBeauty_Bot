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

# ------------------- НАСТРОЙКИ УВЕДОМЛЕНИЙ -------------------
# ADMIN_CHAT_ID: чат/группа для дублей. Если это группа, обычно id начинается с -100...
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "5616469242"))
ONLINE_BOOKING_URL = os.getenv("ONLINE_BOOKING_URL", "https://n561655.yclients.com/")
BOOKING_ENABLED = os.getenv("BOOKING_ENABLED", "false").lower() == "true"

# ---------------------------------------------------------------------
# YCLIENTS webhook (создание/изменение записи)
# ---------------------------------------------------------------------
YCLIENTS_WEBHOOK_SECRET = os.getenv("YCLIENTS_WEBHOOK_SECRET", "")  # можно пустым, но лучше задать

def extract_from_yclients_webhook(payload: dict) -> dict:
    """
    Пытаемся вытащить из webhook хоть что-то полезное.
    Структуры у YCLIENTS бывают разные, поэтому делаем максимально устойчиво.
    """
    # Иногда событие лежит в payload["data"]
    d = payload.get("data") if isinstance(payload.get("data"), dict) else payload

    # record id
    record_id = d.get("id") or d.get("record_id") or d.get("appointment_id") or d.get("event_id")
    record_id = safe_str(record_id)

    # phone
    phone_raw = None
    if isinstance(d.get("client"), dict):
        phone_raw = d["client"].get("phone") or d["client"].get("phone_number")
    phone_raw = phone_raw or d.get("phone") or d.get("client_phone")
    phone = normalize_phone(safe_str(phone_raw)) or safe_str(phone_raw)

    # datetime
    start_str = d.get("start_at") or d.get("datetime") or d.get("date_time") or d.get("seance_date")
    start_dt = try_parse_dt(start_str) if start_str else None

    # service / master / price
    service = "УСЛУГА"
    master = ""
    price = ""

    # services может быть списком
    if isinstance(d.get("services"), list) and d["services"]:
        s0 = d["services"][0]
        if isinstance(s0, dict):
            service = s0.get("title") or s0.get("name") or service
            if s0.get("price"):
                price = str(s0.get("price"))
    # service может быть строкой/словарём
    if isinstance(d.get("service"), dict):
        service = d["service"].get("title") or d["service"].get("name") or service
        if d["service"].get("price"):
            price = str(d["service"]["price"])
    elif isinstance(d.get("service"), str):
        service = d["service"]

    if isinstance(d.get("staff"), dict):
        master = d["staff"].get("name") or master
    if isinstance(d.get("master"), dict):
        master = d["master"].get("name") or master
    elif isinstance(d.get("master"), str):
        master = d["master"]

    # общая цена
    if not price:
        price = safe_str(d.get("price") or d.get("cost") or "")

    return {
        "record_id": record_id,
        "phone": phone,
        "start_dt": start_dt,
        "service": safe_str(service),
        "master": safe_str(master),
        "price": safe_str(price),
        "raw": payload,
    }

@app.post("/yclients-webhook")
async def yclients_webhook(request: Request):
    # защита секретом (рекомендую)
    secret = request.query_params.get("secret", "")
    if YCLIENTS_WEBHOOK_SECRET and secret != YCLIENTS_WEBHOOK_SECRET:
        return JSONResponse(status_code=403, content={"ok": False, "error": "forbidden"})

    payload = await request.json()
    logger.info(f"YCLIENTS webhook: {payload}")

    f = extract_from_yclients_webhook(payload)

    # если не смогли достать телефон — сообщаем админу и выходим
    if not f["phone"]:
        await notify_admin(f"<b>YCLIENTS webhook</b><br/>Не нашла телефон клиента в payload.<br/><pre>{escape_html(json.dumps(payload, ensure_ascii=False)[:1500])}</pre>")
        return {"ok": True}

    phone_map = phone_to_chat_map()
    chat_id = phone_map.get(str(f["phone"]))

    if not chat_id:
        await notify_admin(
            f"<b>Новая запись в YCLIENTS</b><br/>"
            f"Телефон: <code>{escape_html(f['phone'])}</code><br/>"
            f"Но клиент не привязан к боту (не отправлял номер)."
        )
        return {"ok": True}

    # формируем “отбивку”
    if f["start_dt"]:
        time_line = f["start_dt"].strftime("%H:%M")
        dt_line = f"Дата и время визита: {f['start_dt'].strftime('%d.%m.%Y')} {time_line}"
    else:
        dt_line = "Дата и время визита: уточните у администратора"

    price_txt = f"Предварительная cтoимoсть: {f['price']}" if f["price"] else "Предварительная cтoимoсть: —"
    master_txt = f"{f['master']}" if f["master"] else "*к какому Mастеру*"

    msg = tpl_booking_created(
        service=f["service"] or "УСЛУГА",
        master=master_txt,
        price=price_txt,
        dt_str=dt_line,
    )
    await send_client(chat_id, msg, meta="BOOKING_CREATED_WEBHOOK")

    # чтобы не слать повторно
    if f["record_id"]:
        mark_sent(f["record_id"], "created", {"src": "webhook"})

    return {"ok": True}

def is_admin_chat(chat_id: int) -> bool:
    return ADMIN_CHAT_ID != 0 and chat_id == ADMIN_CHAT_ID

def client_label(user: dict, data: dict | None = None) -> str:
    data = data or {}
    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    name = (first + " " + last).strip() or "Без имени"
    uname = user.get("username")
    tg_id = user.get("id")
    phone = data.get("phone")
    parts = [name]
    if uname:
        parts.append(f"@{uname}")
    if tg_id:
        parts.append(f"tg_id={tg_id}")
    if phone:
        parts.append(f"тел={phone}")
    return " | ".join(parts)

async def notify_admin(text: str):
    if ADMIN_CHAT_ID == 0:
        return
    await tg_post("sendMessage", {
        "chat_id": ADMIN_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    })

async def send_client(chat_id: int, text: str, reply_markup: dict | None = None, meta: str | None = None):
    res = await send_message(chat_id, text, reply_markup)
    if not is_admin_chat(chat_id):
        ok = bool(res.get("ok"))
        status = "ОТПРАВЛЕНО" if ok else "ОШИБКА"
        meta_txt = f"<b>{meta}</b>\n" if meta else ""
        await notify_admin(
            f"""{meta_txt}<b>➡️ Исходящее клиенту</b>
chat_id: <code>{chat_id}</code>
Статус: <b>{status}</b>

{text}"""
        )
    return res


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
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
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
def confirm_kb(appt_key: str):
    return inline_keyboard([
        [{"text": "✅ Подтверждаю", "callback_data": f"appt:confirm:{appt_key}"}],
        [{"text": "🔁 Перенести", "callback_data": f"appt:reschedule:{appt_key}"}],
        [{"text": "❌ Отменить", "callback_data": f"appt:cancel:{appt_key}"}],
    ])

def main_menu():
    return inline_keyboard([
        [{"text": "📅 Онлайн-запись", "url": ONLINE_BOOKING_URL}],
        [{"text": "💬 Написать администратору", "callback_data": "menu:to_admin"}],
        [{"text": "📱 Привязать номер", "callback_data": "menu:link_phone"}],
    ])

async def show_welcome(chat_id: int):
    text = """Здравствуйте 🌸
Я — виртуальный администратор студии KUTIKULA.

Я могу присылать вам напоминание о Вашей записи. За три дня, за один день и за пару часов до записи.

Изменить запись вы сможете с помощью онлайн записи перейдя по ссылке:
https://n561655.yclients.com/"""
    await send_client(chat_id, text, main_menu(), meta="WELCOME")
    reset_state(chat_id)



# ------------------- ШАБЛОНЫ СООБЩЕНИЙ -------------------
# Примечание: форматирование — Telegram Markdown (звёздочки *жирный*, подчёркивания _курсив_)

TPL_ON_BOOKING = """👋 Вы записaны в 
Studio KUTIKULA 

▫️*{service}*
*к {master}*
*Предварительная cтoимoсть: {price}*
*Дата и время визита: {dt}*

Aдрес cтудии 
ул. Фасаднaя, д. 21

Вхoд сo стороны улицы Фacaдная
Яндeкc.Карты
https://kutikula116.clients.site 

Ждём Bаc!"""

TPL_REMINDER_3D = """Дoбрый вeчер! 
Hа cвязи Nail Studio KUTIKULA

Hапоминaем, чтo Вы записaны 
*{day_label}* 
*нa {time_hm}* 
▫️*{service}*

Aдрec cтyдии:
yл. Фacаднaя, 21
_вxoд cо стoрoны yл. Фaсадной_

Cсылка на Яндекc.Kaрты:
https://kutikula116.clients.site

*Пожалyйcтa, отправьтe:*
*«+» — если пoдтверждаeте визит*
*«–» — eсли xотитe oтмeнить или перeнеcти запись*"""


# Напоминание за 1 день — используем тот же шаблон (при необходимости можно заменить текстом "завтра")
TPL_REMINDER_1D = TPL_REMINDER_3D

TPL_CANCELLED = """Вaша зaпиcь
▫️*{service}*
нa *{dt}* oтмeнeнa.

Вы мoжeте выбрать удобнoе для себя врeмя, вocпoльзовaвшиcь онлaйн-зaписью пeрeйдя по ccылкe:
*https://n561655.yclients.com/*"""

TPL_REMINDER_2H = """⏳ Ждём Baс в *{time_hm}*

*Пoжaлyйстa, отпрaвьтe:*
*«+» — еcли пoдтверждaетe визит*
*«–» — ecли xотитe oтменить или пeрeнеcти зaпись*"""

RU_WEEK_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

def fmt_day_full(d: date) -> str:
    return f"{RU_WEEK_FULL[d.weekday()]} {d.day} {RU_MONTH[d.month]}"

def hm_from_dt(dt_str: str) -> str:
    # ожидаем "YYYY-MM-DD HH:MM" или "HH:MM"
    if not dt_str:
        return ""
    m = re.search(r"(\d{2}:\d{2})", dt_str)
    return m.group(1) if m else dt_str

def ymd_from_dt(dt_str: str) -> str:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", dt_str)
    return m.group(1) if m else ""

async def send_reminder_3d(chat_id: int, appt_key: str, service: str, dt_str: str):
    ymd = ymd_from_dt(dt_str)
    d = datetime.strptime(ymd, "%Y-%m-%d").date() if ymd else date.today()
    msg = TPL_REMINDER_3D.format(
        day_label=fmt_day_full(d),
        time_hm=hm_from_dt(dt_str),
        service=service,
    )
    # ждём от клиента + / -
    st = get_state(chat_id)
    data = st.get("data", {})
    data["await_appt_key"] = appt_key
    data["await_appt_service"] = service
    data["await_appt_dt"] = dt_str
    set_state(chat_id, "await_plusminus", data)
    await send_client(chat_id, msg, meta="REMINDER_3D")


async def send_reminder_1d(chat_id: int, appt_key: str, service: str, dt_str: str):
    ymd = ymd_from_dt(dt_str)
    d = datetime.strptime(ymd, "%Y-%m-%d").date() if ymd else date.today()
    msg = TPL_REMINDER_1D.format(
        day_label=fmt_day_full(d),
        time_hm=hm_from_dt(dt_str),
        service=service,
    )
    st = get_state(chat_id)
    data = st.get("data", {})
    data["await_appt_key"] = appt_key
    data["await_appt_service"] = service
    data["await_appt_dt"] = dt_str
    set_state(chat_id, "await_plusminus", data)
    await send_client(chat_id, msg, meta="REMINDER_1D")

async def send_reminder_2h(chat_id: int, appt_key: str, dt_str: str):
    msg = TPL_REMINDER_2H.format(time_hm=hm_from_dt(dt_str))
    st = get_state(chat_id)
    data = st.get("data", {})
    data["await_appt_key"] = appt_key
    data["await_appt_dt"] = dt_str
    set_state(chat_id, "await_plusminus", data)
    await send_client(chat_id, msg, meta="REMINDER_2H")
def contact_keyboard():
    return {
        "keyboard": [[{"text": "📱 Отправить номер", "request_contact": True}]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


async def handle_menu(chat_id: int, action: str):
    if action == "link_phone":
        st = get_state(chat_id)
        set_state(chat_id, "await_contact", st.get("data", {}))
        await send_client(
            chat_id,
            "Нажмите кнопку ниже, чтобы отправить номер телефона (нужно для напоминаний о записи).",
            reply_markup=contact_keyboard(),
            meta="LINK_PHONE",
        )
        return

    if action == "to_admin":
        st = get_state(chat_id)
        set_state(chat_id, "chat_to_admin", st.get("data", {}))
        await send_client(chat_id, "Напишите сообщение — я перешлю администратору.", meta="TO_ADMIN")
        return

    # Старый функционал записи оставлен на потом и отключён по умолчанию
    if action in ("book", "services") and not BOOKING_ENABLED:
        await send_client(
            chat_id,
            f"Запись через бота отключена. Используйте онлайн-запись: {ONLINE_BOOKING_URL}",
            main_menu(),
            meta="BOOKING_DISABLED",
        )
        return

    # Если вы включите BOOKING_ENABLED=true — ниже остаётся ваш старый сценарий
    if action == "book":
        cats = await get_categories()
        if not cats:
            await send_client(chat_id, "❌ Не получилось получить категории из YCLIENTS.", meta="BOOKING_ERR")
            return

        rows = []
        for c in cats:
            rows.append([{"text": c["title"], "callback_data": f"cat:{c['id']}"}])

        await send_client(chat_id, "Выберите категорию услуг:", inline_keyboard(rows), meta="BOOKING_CAT")
        set_state(chat_id, "choosing_category", {})
        return

    if action == "services":
        cats = await get_categories()
        if not cats:
            await send_client(chat_id, "❌ Не получилось получить категории из YCLIENTS.", meta="SERVICES_ERR")
            return

        msg = "Категории:\n\n" + "\n".join([f"• {c['title']}" for c in cats])
        await send_client(chat_id, msg, meta="SERVICES_LIST")
        return

    await send_client(chat_id, "Не поняла команду. Напишите /start", meta="UNKNOWN_MENU")


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
            # appt actions (подтверждение/перенос/отмена)
            if data.startswith("appt:"):
                _, action, appt_key = data.split(":", 2)
                user = cq.get("from", {})
                st = get_state(chat_id)
                await notify_admin(
                    f"""<b>🧷 Действие по записи</b>
Клиент: {client_label(user, st.get('data', {}))}
Действие: <b>{action}</b>
appt_key: <code>{appt_key}</code>
chat_id: <code>{chat_id}</code>"""
                )
                if action == "confirm":
                    await send_client(chat_id, "Отлично, запись подтверждена. Ждём вас!", main_menu(), meta="APPT_CONFIRM")
                elif action == "reschedule":
                    await send_client(chat_id, f"Чтобы перенести запись, выберите удобное время онлайн: {ONLINE_BOOKING_URL}", main_menu(), meta="APPT_RESCHEDULE")
                else:
                    await send_client(chat_id, "Запрос на отмену принят. Администратор свяжется, если нужно уточнение.", main_menu(), meta="APPT_CANCEL")
                return JSONResponse(content={"ok": True})

            # menu:
            if data.startswith("menu:"):
                action = data.split(":")[1]
                await handle_menu(chat_id, action)
                return JSONResponse(content={"ok": True})

            # если запись отключена — блокируем старые callback-сценарии записи
            if (not BOOKING_ENABLED) and data.startswith(("cat:", "svc:", "mst:", "cal:", "date:", "time:")):
                await send_client(chat_id, f"Запись через бота отключена. Используйте онлайн-запись: {ONLINE_BOOKING_URL}", main_menu(), meta="BOOKING_DISABLED")
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

    # 2) обычные сообщения (текст/контакт)
    message = update.get("message")
    if not message:
        return JSONResponse(content={"ok": True})

    chat_id = message["chat"]["id"]

    # чтобы не зациклиться: сообщения из админ-чата не пересылаем обратно админу
    if is_admin_chat(chat_id):
        return JSONResponse(content={"ok": True})

    user = message.get("from", {})
    text = (message.get("text") or "").strip()

    # /chatid — для настройки
    if text == "/chatid":
        await send_message(chat_id, f"chat_id = {chat_id}")
        return JSONResponse(content={"ok": True})

    # контакт (кнопка «Отправить номер»)
    contact = message.get("contact")
    if contact:
        phone_raw = contact.get("phone_number", "")
        phone = normalize_phone(phone_raw) or phone_raw

        st = get_state(chat_id)
        data = st.get("data", {})
        data["phone"] = phone
        set_state(chat_id, "idle", data)

        await notify_admin(
            f"""<b>📱 Клиент отправил контакт</b>
Клиент: {client_label(user, data)}
chat_id: <code>{chat_id}</code>
Телефон: <code>{phone}</code>"""
        )
        await send_client(chat_id, "Спасибо! Номер сохранён.", main_menu(), meta="CONTACT_SAVED")
        return JSONResponse(content={"ok": True})

    # команды старта / приветствия
    text_l = text.lower()
    greetings = {
        "start", "привет", "здравствуйте", "добрый день", "добрый вечер", "доброе утро",
        "hi", "hello",
    }
    if text.startswith("/start") or text_l in greetings or text_l.startswith(("привет", "здравств", "добрый ")):
        await show_welcome(chat_id)
        return JSONResponse(content={"ok": True})

    st = get_state(chat_id)
    step = st.get("step", "idle")
    data = st.get("data", {})

    # входящее сообщение — всегда дублируем админу (если не пустое)
    if text:
        await notify_admin(
            f"""<b>📩 Входящее от клиента</b>
Клиент: {client_label(user, data)}
chat_id: <code>{chat_id}</code>

{text}"""
        )

    # режим «написать администратору»
    if step == "chat_to_admin":
        await send_client(
            chat_id,
            "Сообщение передано администратору. Ответим вам в этом чате.",
            main_menu(),
            meta="MSG_TO_ADMIN_OK",
        )
        set_state(chat_id, "idle", data)
        return JSONResponse(content={"ok": True})

    
    # ожидание подтверждения визита через "+" / "-" (после напоминаний)
    if step == "await_plusminus" and text:
        t = text.strip()
        if t in ("+", "＋"):
            appt_key = data.get("await_appt_key")
            appt_dt = data.get("await_appt_dt")
            appt_service = data.get("await_appt_service")
            await notify_admin(
                f"""<b>✅ Клиент подтвердил визит</b>
Клиент: {client_label(user, data)}
chat_id: <code>{chat_id}</code>
appt_key: <code>{appt_key}</code>
Услуга: {appt_service or "-"}
Дата/время: {appt_dt or "-"}"""
            )
            await send_client(chat_id, "Спасибо! Визит подтверждён.", main_menu(), meta="PLUS_CONFIRM")
            # очищаем ожидание
            data.pop("await_appt_key", None)
            data.pop("await_appt_dt", None)
            data.pop("await_appt_service", None)
            set_state(chat_id, "idle", data)
            return JSONResponse(content={"ok": True})

        if t in ("-", "–", "—"):
            appt_key = data.get("await_appt_key")
            appt_dt = data.get("await_appt_dt")
            appt_service = data.get("await_appt_service")
            await notify_admin(
                f"""<b>❌ Клиент просит отменить/перенести</b>
Клиент: {client_label(user, data)}
chat_id: <code>{chat_id}</code>
appt_key: <code>{appt_key}</code>
Услуга: {appt_service or "-"}
Дата/время: {appt_dt or "-"}"""
            )
            await send_client(
                chat_id,
                f"Принято. Отменить или перенести запись можно через онлайн-запись:\n{ONLINE_BOOKING_URL}",
                main_menu(),
                meta="MINUS_CANCEL_RESCHEDULE",
            )
            data.pop("await_appt_key", None)
            data.pop("await_appt_dt", None)
            data.pop("await_appt_service", None)
            set_state(chat_id, "idle", data)
            return JSONResponse(content={"ok": True})

# если пользователь прислал номер текстом
    ph = normalize_phone(text)
    if ph:
        data["phone"] = ph
        set_state(chat_id, "idle", data)
        await notify_admin(
            f"""<b>📱 Клиент прислал номер текстом</b>
Клиент: {client_label(user, data)}
chat_id: <code>{chat_id}</code>
Телефон: <code>{ph}</code>"""
        )
        await send_client(chat_id, "Спасибо! Номер сохранён.", main_menu(), meta="PHONE_SAVED_TEXT")
        return JSONResponse(content={"ok": True})

    # по умолчанию
    await send_client(
        chat_id,
        "Принято.\n\nЧтобы записаться — нажмите «Онлайн-запись».\nЕсли нужен администратор — нажмите «Написать администратору».",
        main_menu(),
        meta="DEFAULT_REPLY",
    )
    return JSONResponse(content={"ok": True})
