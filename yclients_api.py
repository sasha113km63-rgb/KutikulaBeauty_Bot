import os
import requests

# Загружаем переменные окружения (Render Environment)
YCLIENTS_API_URL = os.getenv("YCLIENTS_API_BASE")
COMPANY_ID = os.getenv("YCLIENTS_COMPANY_ID")
PARTNER_TOKEN = os.getenv("YCLIENTS_PARTNER_TOKEN")
USER_TOKEN = os.getenv("YCLIENTS_USER_TOKEN")

# Заголовки авторизации
HEADERS = {
    "Accept": "application/vnd.yclients.v2+json",
    "Content-Type": "application/json",
    "Authorization": f"Bearer {PARTNER_TOKEN}"
}

# --- Получить список услуг ---
def get_services():
    url = f"{YCLIENTS_API_URL}/book_services/{COMPANY_ID}"
    response = requests.get(url, headers=HEADERS)
    return response.json()

# --- Получить список сотрудников (мастеров) ---
def get_staff(service_id=None):
    url = f"{YCLIENTS_API_URL}/book_staff/{COMPANY_ID}"
    params = {}
    if service_id:
        params["service_ids[]"] = service_id
    response = requests.get(url, headers=HEADERS, params=params)
    return response.json()

# --- Получить доступные времена ---
def get_available_times(staff_id, date, service_id):
    url = f"{YCLIENTS_API_URL}/book_times/{COMPANY_ID}/{staff_id}/{date}"
    params = {"service_ids[]": service_id}
    response = requests.get(url, headers=HEADERS, params=params)
    return response.json()

# --- Создать запись ---
def create_appointment(staff_id, service_id, datetime):
    url = f"{YCLIENTS_API_URL}/book_record/{COMPANY_ID}"
    payload = {
        "appointments": [
            {
                "services": [service_id],
                "staff_id": staff_id,
                "datetime": datetime
            }
        ]
    }
    response = requests.post(url, headers=HEADERS, json=payload)
    return response.json()

# --- Проверить параметры перед записью ---
def check_appointment(staff_id, service_id, datetime):
    url = f"{YCLIENTS_API_URL}/book_check/{COMPANY_ID}"
    payload = {
        "appointments": [
            {
                "services": [service_id],
                "staff_id": staff_id,
                "datetime": datetime
            }
        ]
    }
    response = requests.post(url, headers=HEADERS, json=payload)
    return response.json()

# --- Получить записи пользователя ---
def get_user_records():
    headers = {
        **HEADERS,
        "Authorization": f"Bearer {PARTNER_TOKEN}, User {USER_TOKEN}"
    }
    url = f"{YCLIENTS_API_URL}/user/records"
    response = requests.get(url, headers=headers)
    return response.json()

# --- Удалить запись пользователя ---
def delete_user_record(record_id, record_hash):
    headers = {
        **HEADERS,
        "Authorization": f"Bearer {PARTNER_TOKEN}, User {USER_TOKEN}"
    }
    url = f"{YCLIENTS_API_URL}/user/records/{record_id}/{record_hash}"
    response = requests.delete(url, headers=headers)
    return response.json()
