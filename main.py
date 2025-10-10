import os
import json
from fastapi import FastAPI, Request, BackgroundTasks
import httpx
from typing import Optional

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
YCLIENTS_USER_TOKEN = os.environ.get("YCLIENTS_USER_TOKEN")
YCLIENTS_COMPANY_ID = os.environ.get("YCLIENTS_COMPANY_ID")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
YCLIENTS_API_BASE = os.environ.get("YCLIENTS_API_BASE", "https://api.yclients.com")

app = FastAPI(title="KUTIKULA Bot")
user_states = {}

async def send_telegram_message(chat_id: int, text: str, parse_mode: Optional[str] = "HTML"):
    if not TELEGRAM_TOKEN:
        print("Ошибка: TELEGRAM_TOKEN не задан")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    async with httpx.AsyncClient() as client:
        await client.post(url, json=payload)

async def get_services():
    url = f"{YCLIENTS_API_BASE}/api/v1/company/{YCLIENTS_COMPANY_ID}/services"
    headers = {"Authorization": f"Bearer {YCLIENTS_USER_TOKEN}"}
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=headers)
        if r.status_code == 200:
            return r.json().get("data", [])
        else:
            print("Ошибка получения услуг:", r.text)
            return []

async def get_available_slots(service_id: int):
    url = f"{YCLIENTS_API_BASE}/api/v1/book_times/{YCLIENTS_COMPANY_ID}"
    headers = {"Authorization": f"Bearer {YCLIENTS_USER_TOKEN}"}
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=headers, params={"service_ids": service_id})
        if r.status_code == 200:
            data = r.json().get("data", [])
            return data[:3]
        else:
            print("Ошибка получения слотов:", r.text)
            return []

async def create_yclients_booking(service_id: int, date_time: str, client_name: str, client_phone: str):
    url = f"{YCLIENTS_API_BASE}/api/v1/book_record/{YCLIENTS_COMPANY_ID}"
    payload = {
        "services": [{"id": service_id}],
        "client": {"name": client_name, "phone": client_phone},
        "datetime": date_time,
    }
    headers = {"Authorization": f"Bearer {YCLIENTS_USER_TOKEN}", "Content-Type": "application/json"}
    async with httpx.AsyncClient() as client:
        r = await client.post(url, headers=headers, json=payload)
        if r.status_code in (200, 201):
            return True, r.json()
        else:
            return False, {"status": r.status_code, "text": r.text}

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    message = data.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "").strip()

    if not text:
        return {"ok": True}

    state = user_states.get(chat_id, {})

    if text.lower() in ["привет", "записаться", "хочу запись", "start", "/start"]:
        user_states[chat_id] = {"step": "choose_service"}
        services = await get_services()
        if not services:
            await send_telegram_message(chat_id, "😔 Не удалось загрузить список услуг. Попробуйте позже.")
            return {"ok": True}
        service_list = "\n".join([f"{i+1}. {s['title']}" for i, s in enumerate(services)])
        await send_telegram_message(chat_id, f"💅 Выберите услугу:\n{service_list}\n\nНапишите номер услуги.")
        user_states[chat_id]["services"] = services
        return {"ok": True}

    if state.get("step") == "choose_service":
        services = state.get("services", [])
        try:
            index = int(text) - 1
            chosen_service = services[index]
            user_states[chat_id]["chosen_service"] = chosen_service
            user_states[chat_id]["step"] = "choose_time"

            slots = await get_available_slots(chosen_service["id"])
            if not slots:
                await send_telegram_message(chat_id, "😔 Нет доступных времён, попробуйте позже.")
                return {"ok": True}

            slot_text = "\n".join([f"{i+1}. {s}" for i, s in enumerate(slots)])
            await send_telegram_message(chat_id, f"⏰ Доступные окошки:\n{slot_text}\n\nНапишите номер времени.")
            user_states[chat_id]["slots"] = slots
            return {"ok": True}
        except:
            await send_telegram_message(chat_id, "⚠️ Введите номер услуги из списка.")
            return {"ok": True}

    if state.get("step") == "choose_time":
        try:
            slots = state.get("slots", [])
            index = int(text) - 1
            chosen_slot = slots[index]
            user_states[chat_id]["chosen_slot"] = chosen_slot
            user_states[chat_id]["step"] = "enter_name"
            await send_telegram_message(chat_id, "✏️ Укажите, пожалуйста, ваше имя.")
            return {"ok": True}
        except:
            await send_telegram_message(chat_id, "⚠️ Введите номер из списка времён.")
            return {"ok": True}

    if state.get("step") == "enter_name":
        user_states[chat_id]["client_name"] = text
        user_states[chat_id]["step"] = "enter_phone"
        await send_telegram_message(chat_id, "📞 Укажите ваш номер телефона (в формате +79991234567).")
        return {"ok": True}

    if state.get("step") == "enter_phone":
        user_states[chat_id]["client_phone"] = text
        info = user_states[chat_id]
        success, resp = await create_yclients_booking(
            service_id=info["chosen_service"]["id"],
            date_time=info["chosen_slot"],
            client_name=info["client_name"],
            client_phone=info["client_phone"],
        )

        if success:
            await send_telegram_message(chat_id, "✅ Вы успешно записаны! Спасибо 🌸")
            if ADMIN_CHAT_ID:
                msg = (
                    f"📅 Новая запись:\n"
                    f"{info['client_name']} ({info['client_phone']})\n"
                    f"Услуга: {info['chosen_service']['title']}\n"
                    f"Время: {info['chosen_slot']}"
                )
                background_tasks.add_task(send_telegram_message, ADMIN_CHAT_ID, msg)
        else:
            await send_telegram_message(chat_id, "❗️ Не удалось записать. Попробуйте позже.")
        user_states.pop(chat_id, None)
        return {"ok": True}

    await send_telegram_message(chat_id, "👋 Напишите 'записаться', чтобы начать запись.")
    return {"ok": True}

@app.get("/")
async def root():
    return {"status": "ok"}
