
import os
import json
from fastapi import FastAPI, Request, BackgroundTasks
import httpx
from typing import Optional

# ПРИМЕЧАНИЕ:
# Токены и чувствительные параметры берутся из переменных окружения (Render env vars).

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
YCLIENTS_USER_TOKEN = os.environ.get("YCLIENTS_USER_TOKEN")
YCLIENTS_COMPANY_ID = os.environ.get("YCLIENTS_COMPANY_ID")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")  # Telegram chat ID администратора
BASE_URL = os.environ.get("BASE_URL")  # https://your-service.onrender.com
YCLIENTS_API_BASE = os.environ.get("YCLIENTS_API_BASE", "https://api.yclients.com")

app = FastAPI(title="KUTIKULA Bot")

async def send_telegram_message(chat_id: int, text: str, parse_mode: Optional[str] = "HTML"):
    if not TELEGRAM_TOKEN:
        print("TELEGRAM_TOKEN not set")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=payload, timeout=15.0)
        try:
            r.raise_for_status()
        except Exception:
            print("Failed to send telegram message:", r.text)

@app.on_event("startup")
async def startup_event():
    if TELEGRAM_TOKEN and BASE_URL:
        webhook_url = f"{BASE_URL.rstrip('/')}/telegram-webhook"
        set_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
        async with httpx.AsyncClient() as client:
            r = await client.post(set_url, json={"url": webhook_url})
            print("setWebhook response:", r.text)

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    message = data.get("message") or data.get("edited_message")
    if not message:
        return {"ok": True}
    chat_id = message["chat"]["id"]
    text = message.get("text","").strip()
    if text.startswith("/start"):
        reply = (
            "Здравствуйте! Это виртуальный администратор студии KUTIKULA.\n\n"
            "Я помогу подобрать услугу, записать вас к мастеру и ответить на вопросы по ценам и графику.\n\n"
            "Выберите действие или напишите, чем могу помочь 💅\n\n"
            "Команды:\n"
            "/services - показать основные услуги\n"
            "/book - начать запись\n"
        )
        background_tasks.add_task(send_telegram_message, chat_id, reply)
        return {"ok": True}

    if text.startswith("/services"):
        services_text = (
            "Наши основные услуги:\n\n"
            "💇‍♀️ Парикмахерские услуги: стрижки, окрашивание, уход\n"
            "💅 Ногтевой сервис: маникюр, педикюр, покрытие гель-лаком\n"
            "✨ Лазерная эпиляция: зоны лица, рук, ног, тела\n\n"
            "Чтобы записаться, напишите /book\n"
        )
        background_tasks.add_task(send_telegram_message, chat_id, services_text)
        return {"ok": True}

    if text.startswith("/book"):
        prompt = (
            "Хорошо. Чтобы записать вас, укажите, пожалуйста, услугу (например: 'маникюр' или 'лазер эпиляция ноги'),\n"
            "дату в формате ДД.MM.ГГГГ и удобное время (например, 15:00), а также контактное имя и телефон.\n\n"
            "Пример: маникюр, 25.11.2025, 15:00, Анна, +79161234567"
        )
        background_tasks.add_task(send_telegram_message, chat_id, prompt)
        return {"ok": True}

    parts = [p.strip() for p in text.split(",")]
    if len(parts) >= 4:
        service_name = parts[0]
        date_str = parts[1]
        time_str = parts[2]
        client_name = parts[3]
        client_phone = parts[4] if len(parts) >=5 else ""
        success, resp = await create_yclients_booking(service_name, date_str, time_str, client_name, client_phone)
        if success:
            reply = f"✅ Ваша запись успешно создана: {service_name}, {date_str} {time_str}.\nМы свяжемся с Вами для подтверждения. Спасибо 🌸"
            background_tasks.add_task(send_telegram_message, chat_id, reply)
            if ADMIN_CHAT_ID:
                admin_text = f"Новая запись создана через бота:\n{service_name}\n{date_str} {time_str}\n{client_name}\n{client_phone}\n\nОтвет YCLIENTS:\n<pre>{json.dumps(resp, ensure_ascii=False, indent=2)}</pre>"
                background_tasks.add_task(send_telegram_message, ADMIN_CHAT_ID, admin_text, "HTML")
        else:
            reply = "❗️ Не удалось создать запись. Попробуйте еще раз или напишите администратору."
            background_tasks.add_task(send_telegram_message, chat_id, reply)
        return {"ok": True}

    background_tasks.add_task(send_telegram_message, chat_id, "Спасибо! Я получил ваше сообщение и скоро отвечу ✨")
    return {"ok": True}

async def create_yclients_booking(service_name: str, date_str: str, time_str: str, client_name: str, client_phone: str):
    if not (YCLIENTS_USER_TOKEN and YCLIENTS_COMPANY_ID):
        return False, {"error": "YCLIENTS credentials not set"}

    url = f"{YCLIENTS_API_BASE}/api/v1/companies/{YCLIENTS_COMPANY_ID}/bookings"

    payload = {
        "client": {
            "name": client_name,
            "phone": client_phone
        },
        "service": {
            "name": service_name
        },
        "datetime": f"{date_str} {time_str}"
    }

    headers = {"Authorization": f"Bearer {YCLIENTS_USER_TOKEN}", "Content-Type": "application/json"}
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(url, headers=headers, json=payload, timeout=15.0)
            content = r.json()
            if r.status_code in (200,201):
                return True, content
            else:
                print("YCLIENTS booking failed:", r.status_code, r.text)
                return False, {"status": r.status_code, "text": r.text, "json": content}
        except Exception as e:
            print("YCLIENTS request exception:", str(e))
            return False, {"exception": str(e)}

@app.post("/yclients-webhook")
async def yclients_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    pretty = json.dumps(payload, ensure_ascii=False, indent=2)
    text = f"📣 Новое событие из YCLIENTS:\n<pre>{pretty}</pre>"
    if ADMIN_CHAT_ID:
        background_tasks.add_task(send_telegram_message, ADMIN_CHAT_ID, text, "HTML")
    return {"status": "ok"}
