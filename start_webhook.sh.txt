#!/bin/bash
TOKEN="<TELEGRAM_TOKEN>"
BASE_URL="<BASE_URL>"
curl -X POST "https://api.telegram.org/bot$TOKEN/setWebhook" -d "url=${BASE_URL}/telegram-webhook"
echo "Webhook set to ${BASE_URL}/telegram-webhook"
