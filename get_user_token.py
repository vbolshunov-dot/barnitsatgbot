#!/usr/bin/env python3
"""Получает User-токен YCLIENTS и дописывает его в .env.

Запускать из терминала: python3 get_user_token.py
Пароль вводится скрыто и никуда, кроме запроса в YCLIENTS, не уходит.
Сам токен на экран не печатается — только последние 4 символа для сверки.
"""
import getpass
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ENV_PATH = Path(__file__).with_name(".env")
load_dotenv(ENV_PATH)

partner_token = os.getenv("YCLIENTS_TOKEN", "")
if not partner_token:
    sys.exit("В .env нет YCLIENTS_TOKEN (партнёрский токен). Сначала задайте его.")

login = input("Логин YCLIENTS (email или телефон сотрудника): ").strip()
password = getpass.getpass("Пароль: ")

resp = httpx.post(
    "https://api.yclients.com/api/v1/auth",
    json={"login": login, "password": password},
    headers={
        "Authorization": f"Bearer {partner_token}",
        "Accept": "application/vnd.yclients.v2+json",
        "Content-Type": "application/json",
    },
    timeout=15.0,
)

if resp.status_code != 201 and resp.status_code != 200:
    sys.exit(f"YCLIENTS ответил {resp.status_code}: {resp.text}")

data = resp.json().get("data") or {}
token = data.get("user_token")
if not token:
    sys.exit(f"В ответе нет user_token: {resp.text}")

lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
lines = [line for line in lines if not line.startswith("YCLIENTS_USER_TOKEN=")]
lines.append(f"YCLIENTS_USER_TOKEN={token}")
ENV_PATH.write_text("\n".join(lines) + "\n")

print(f"Готово. Токен для {data.get('name', login)} записан в {ENV_PATH}")
print(f"Оканчивается на ...{token[-4:]} — для сверки, если понадобится.")
