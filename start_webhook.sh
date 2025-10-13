#!/bin/bash
TOKEN="7674592997:AAEWsGG55yVxcnH5RoaHNHRdDLRkDpiNLmkE"
BASE_URL="https://kutikulabeauty-bot.onrender.com"

curl -X POST "https://api.telegram.org/bot${TOKEN}/setWebhook" \
     -d "url=${BASE_URL}/telegram-webhook"

echo "Webhook set to ${BASE_URL}/telegram-webhook"
