# KUTIKULA Telegram Bot — инструкция (на русском)

Это минимальный бот-веб-сервис для студии красоты KUTIKULA.
Бот:
- отвечает пользователю в Telegram (русский язык);
- может создавать записи в YCLIENTS (через API);
- принимает webhooks от YCLIENTS.

---

## Что внутри
- `main.py` — основной FastAPI-приложение
- `requirements.txt` — зависимости
- `.env.example` — пример переменных окружения

---

## Перед развёртыванием (важно)
1. Подготовьте токены:
   - TELEGRAM_TOKEN — токен бота от @BotFather
   - YCLIENTS_USER_TOKEN — User Token из YCLIENTS (в настройках API вашего аккаунта)
   - YCLIENTS_COMPANY_ID — ID вашей компании (пример: 11673)
   - ADMIN_CHAT_ID — ваш Telegram chat id (чтобы приходили уведомления администратору)
   - BASE_URL — публичный адрес сервиса (после деплоя на Render)

2. Если вы не знаете ADMIN_CHAT_ID — напишите боту любое сообщение и получите ID через:
   - Введите в браузере: https://api.telegram.org/bot<TELEGRAM_TOKEN>/getUpdates
   - В результате поиска в JSON найдите "chat": {"id": 123456789, ...} — это и есть ваш ADMIN_CHAT_ID
   - Или используйте @userinfobot в Telegram

---

## Развёртывание на Render (шаги для новичка)
1. Зайдите в ваш аккаунт Render.
2. Нажмите **New → Web Service**.
3. Выберите "Deploy from ZIP" (или "Deploy from GitHub" если предпочитаете).
4. Загрузите этот ZIP-архив с проектом.
5. В разделе **Environment** добавьте переменные:
   - TELEGRAM_TOKEN
   - YCLIENTS_USER_TOKEN
   - YCLIENTS_COMPANY_ID
   - ADMIN_CHAT_ID
   - BASE_URL (после первого деплоя укажите адрес вида https://your-service.onrender.com)
6. В поле **Start Command** укажите:
   ```
   gunicorn -w 4 -k uvicorn.workers.UvicornWorker main:app
   ```
7. Нажмите **Create Web Service** → дождитесь деплоя.
8. После успешного деплоя установите BASE_URL (если ещё не указали) и перезапустите сервис.

---

## Установка webhook в YCLIENTS
В YCLIENTS вставьте Webhook URL:
```
<BASE_URL>/yclients-webhook
```

---

## Проверка работы
1. В Telegram напишите боту `/start` — бот должен ответить.
2. Команда `/services` — покажет список услуг.
3. Команда `/book` — бот подскажет формат записи.
4. Для теста записи отправьте боту сообщение в формате:
   маникюр, 25.11.2025, 15:00, Анна, +79161234567

---

## Важные заметки
- API YCLIENTS может иметь нюансы в теле запроса для создания записи. Если при попытке записи приходит ошибка — откройте логи Render и посмотрите ответ API (в main.py выводится текст ошибки).
- Если необходимо, я помогу адаптировать тело запроса для точной структуры вашего аккаунта YCLIENTS.
