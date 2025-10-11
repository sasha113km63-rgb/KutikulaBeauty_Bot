import os
import requests
import logging

YCLIENTS_API_BASE = os.getenv("YCLIENTS_API_BASE", "https://api.yclients.com")
YCLIENTS_COMPANY_ID = os.getenv("YCLIENTS_COMPANY_ID")
YCLIENTS_PARTNER_TOKEN = os.getenv("YCLIENTS_PARTNER_TOKEN")
YCLIENTS_USER_TOKEN = os.getenv("YCLIENTS_USER_TOKEN")

def get_services():
    """
    Получает список услуг из YCLIENTS через API и логирует подробности.
    """
    url = f"{YCLIENTS_API_BASE}/api/v1/company/{YCLIENTS_COMPANY_ID}/services"
    headers = {
        "Accept": "application/vnd.yclients.v2+json",
        "Authorization": f"Bearer {YCLIENTS_PARTNER_TOKEN}, User {YCLIENTS_USER_TOKEN}"
    }

    logging.info("🔍 Отправка запроса к YCLIENTS API: %s", url)
    logging.info("🔑 Токены: PARTNER=%s..., USER=%s...", YCLIENTS_PARTNER_TOKEN[:6], YCLIENTS_USER_TOKEN[:6])

    try:
        response = requests.get(url, headers=headers, timeout=15)
        logging.info("📩 Ответ YCLIENTS: статус %s", response.status_code)

        if response.status_code == 200:
            data = response.json()
            services = data.get("data", [])
            logging.info("✅ Получено %s услуг", len(services))
            return services
        else:
            logging.error("❌ Ошибка при получении услуг: %s — %s", response.status_code, response.text)
            return []
    except Exception as e:
        logging.exception("⚠️ Исключение при запросе к YCLIENTS: %s", e)
        return []
