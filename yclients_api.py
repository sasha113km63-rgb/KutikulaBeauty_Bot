import requests
from config import YCLIENTS_API_URL, COMPANY_ID, BEARER_TOKEN

# --- Заголовки для авторизации ---
HEADERS = {
    "Accept": "application/vnd.yclients.v2+json",
    "Content-Type": "application/json",
    "Authorization": f"Bearer {BEARER_TOKEN}"
}

# --- Получить список услуг ---
def get_services():
    url = f"{YCLIENTS_API_URL}/services/{COMPANY_ID}"
    r = requests.get(url, headers=HEADERS)
    return r.json()

# --- Получить сотрудников (мастеров) ---
def get_staff(service_id=None):
    url = f"{YCLIENTS_API_URL}/staff/{COMPANY_ID}"
    params = {}
    if service_id:
        params["service_id"] = service_id
    r = requests.get(url, headers=HEADERS, params=params)
    return r.json()

# --- Получить доступное время для записи ---
def get_times(staff_id, date, service_id):
    url = f"{YCLIENTS_API_URL}/book_times/{COMPANY_ID}"
    payload = {
        "staff_id": staff_id,
        "services": [service_id],
        "date": date
    }
    r = requests.post(url, headers=HEADERS, json=payload)
    return r.json()

# --- Создать запись ---
def create_record(phone, name, staff_id, services, datetime_str):
    url = f"{YCLIENTS_API_URL}/book_record/{COMPANY_ID}"
    payload = {
        "phone": phone,
        "name": name,
        "staff_id": staff_id,
        "services": services,
        "datetime": datetime_str,
        "send_sms": 1
    }
    r = requests.post(url, headers=HEADERS, json=payload)
    return r.json()

# --- Получить список активных записей пользователя ---
def get_company_records():
    url = f"{YCLIENTS_API_URL}/records/{COMPANY_ID}"
    r = requests.get(url, headers=HEADERS)
    return r.json()

# --- Отменить (удалить) запись ---
def delete_record(record_id):
    url = f"{YCLIENTS_API_URL}/records/{COMPANY_ID}/{record_id}"
    r = requests.delete(url, headers=HEADERS)
    if r.status_code == 200:
        return {"success": True}
    else:
        return {"success": False, "error": r.text}
