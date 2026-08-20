#!/usr/bin/env python3
"""Получает User-токен YCLIENTS.

Запускать из терминала: python3 get_user_token.py
Зависимостей нет — только стандартная библиотека, чтобы работало на любом Python 3.

Пароль вводится скрыто и уходит только в YCLIENTS.
Сам токен на экран не печатается: он копируется в буфер обмена (macOS) и, если
рядом есть .env, дописывается туда. В консоль идут лишь последние 4 символа —
чтобы можно было сверить, тот ли токен вставился.
"""
import getpass
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENV_PATH = Path(__file__).with_name(".env")


def read_env_token() -> str:
    """Берёт партнёрский токен из окружения, а если его там нет — из .env рядом."""
    token = os.getenv("YCLIENTS_TOKEN", "")
    if token or not ENV_PATH.exists():
        return token
    for line in ENV_PATH.read_text().splitlines():
        if line.startswith("YCLIENTS_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


partner_token = read_env_token()
if not partner_token:
    print("Не нашёл партнёрский токен YCLIENTS.")
    print("Скопируйте его из панели хостинга (переменная YCLIENTS_TOKEN) и вставьте сюда.")
    partner_token = getpass.getpass("YCLIENTS_TOKEN (ввод скрыт): ").strip()
if not partner_token:
    sys.exit("Без партнёрского токена авторизоваться нельзя.")

login = input("Логин YCLIENTS (email или телефон сотрудника): ").strip()
password = getpass.getpass("Пароль (ввод скрыт): ")

request = urllib.request.Request(
    "https://api.yclients.com/api/v1/auth",
    data=json.dumps({"login": login, "password": password}).encode(),
    headers={
        "Authorization": f"Bearer {partner_token}",
        "Accept": "application/vnd.yclients.v2+json",
        "Content-Type": "application/json",
    },
    method="POST",
)

try:
    with urllib.request.urlopen(request, timeout=15) as response:
        body = json.load(response)
except urllib.error.HTTPError as e:
    detail = e.read().decode(errors="replace")
    sys.exit(f"YCLIENTS ответил {e.code}: {detail}")
except urllib.error.URLError as e:
    sys.exit(f"Не достучались до YCLIENTS: {e.reason}")

data = body.get("data") or {}
token = data.get("user_token")
if not token:
    sys.exit(f"В ответе нет user_token: {json.dumps(body, ensure_ascii=False)}")

who = data.get("name") or login
print(f"\nТокен получен для: {who}")
print(f"Оканчивается на ...{token[-4:]} — по этому хвосту сверите, что вставили верно.")

try:
    subprocess.run(["pbcopy"], input=token.encode(), check=True)
    print("Токен скопирован в буфер обмена — вставьте его в панели хостинга")
    print("как переменную YCLIENTS_USER_TOKEN и перезапустите бота.")
except (OSError, subprocess.CalledProcessError):
    print("Скопировать в буфер не вышло (pbcopy недоступен).")

if ENV_PATH.exists():
    lines = [ln for ln in ENV_PATH.read_text().splitlines()
             if not ln.startswith("YCLIENTS_USER_TOKEN=")]
    lines.append(f"YCLIENTS_USER_TOKEN={token}")
    ENV_PATH.write_text("\n".join(lines) + "\n")
    print(f"Заодно записан в {ENV_PATH.name}.")
