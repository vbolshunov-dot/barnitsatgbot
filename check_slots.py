#!/usr/bin/env python3
"""Диагностика свободного времени в YCLIENTS.

Спрашивает у API одно и то же время двумя способами — с фильтром по услуге
и без него — и показывает разницу. Так видно, режет ли слоты сама услуга
или ограничение сидит глубже, на сотруднике и его графике.

Запускать: python3 check_slots.py [дата] [staff_id] [service_id]
По умолчанию: 2026-08-26, берёзовая (5056080), будни с процедурами (27116352).

Зависимостей нет. Токены берутся из окружения или спрашиваются скрыто,
на экран не выводятся.
"""
import getpass
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

COMPANY_ID = os.getenv("YCLIENTS_COMPANY_ID", "1395168")
DATE = sys.argv[1] if len(sys.argv) > 1 else "2026-08-26"
STAFF_ID = sys.argv[2] if len(sys.argv) > 2 else "5056080"
SERVICE_ID = sys.argv[3] if len(sys.argv) > 3 else "27116352"

ENV_PATH = Path(__file__).with_name(".env")


def env_value(name: str) -> str:
    """Ищет переменную в окружении, затем в .env рядом со скриптом."""
    value = os.getenv(name, "")
    if value or not ENV_PATH.exists():
        return value
    for line in ENV_PATH.read_text().splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    return ""


partner = env_value("YCLIENTS_TOKEN") or getpass.getpass("YCLIENTS_TOKEN (ввод скрыт): ").strip()
user = env_value("YCLIENTS_USER_TOKEN") or getpass.getpass("YCLIENTS_USER_TOKEN (ввод скрыт): ").strip()
if not partner:
    sys.exit("Без партнёрского токена запросить нечего.")

auth = f"Bearer {partner}, User {user}" if user else f"Bearer {partner}"
headers = {"Authorization": auth, "Accept": "application/vnd.yclients.v2+json"}


def ask(label: str, url: str) -> None:
    print(f"\n--- {label}")
    print(f"    {url}")
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.load(response)
            code = response.status
    except urllib.error.HTTPError as e:
        print(f"    HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
        return
    except urllib.error.URLError as e:
        print(f"    не достучались: {e.reason}")
        return

    data = body.get("data") or []
    if label.startswith("карточка филиала") and isinstance(data, dict):
        for key in ("title", "schedule", "timezone", "timezone_name", "zip",
                    "business_type_id", "allow_delete_record", "country_id"):
            if key in data:
                print(f"    {key}: {data[key]}")
        return
    if label.startswith("карточка услуги"):
        # Из карточки интересны только границы окна и период — остальное шум
        def hhmm(seconds):
            return f"{int(seconds) // 3600:02d}:{int(seconds) % 3600 // 60:02d}" if seconds else seconds
        print(f"    название: {data.get('booking_title')}")
        print(f"    окно записи: {hhmm(data.get('seance_search_start'))} - {hhmm(data.get('seance_search_finish'))}"
              f", шаг {data.get('seance_search_step')} сек")
        print(f"    период: {data.get('date_from')} - {data.get('date_to')}"
              f" (ограничение включено: {data.get('is_need_limit_date')})")
        print(f"    длительность: {hhmm(data.get('duration'))}, вес перерыва: {data.get('step')}")
        return
    if isinstance(data, list) and data and isinstance(data[0], dict):
        times = [d.get("time") or d.get("date") for d in data]
        print(f"    HTTP {code}, записей {len(data)}: {times}")
        print(f"    первая как есть: {json.dumps(data[0], ensure_ascii=False)}")
    else:
        print(f"    HTTP {code}, данные: {json.dumps(data, ensure_ascii=False)[:400]}")


base = f"https://api.yclients.com/api/v1"
print(f"Филиал {COMPANY_ID}, сотрудник {STAFF_ID}, услуга {SERVICE_ID}, дата {DATE}")

ask("время С фильтром по услуге (так ходит бот)",
    f"{base}/book_times/{COMPANY_ID}/{STAFF_ID}/{DATE}?service_ids%5B%5D={SERVICE_ID}")
ask("время БЕЗ фильтра по услуге",
    f"{base}/book_times/{COMPANY_ID}/{STAFF_ID}/{DATE}")
ask("доступные даты по услуге",
    f"{base}/book_dates/{COMPANY_ID}?staff_id={STAFF_ID}&service_ids%5B%5D={SERVICE_ID}&date={DATE}")
ask("карточка услуги (длительность и перерыв)",
    f"{base}/company/{COMPANY_ID}/services/{SERVICE_ID}")
ask("график сотрудника глазами API",
    f"{base}/schedule/{COMPANY_ID}/{STAFF_ID}/{DATE}/{DATE}")
ask("карточка сотрудника",
    f"{base}/company/{COMPANY_ID}/staff/{STAFF_ID}")

ask("карточка филиала (часы работы и часовой пояс)",
    f"{base}/company/{COMPANY_ID}?my=1")

# Сравниваем бани между собой: если сетка обрезана у обеих одинаково,
# ограничение общее для филиала, а не привязано к конкретной бане.
print("\n--- сетка у всех бань филиала на эту дату")
try:
    req = urllib.request.Request(f"{base}/company/{COMPANY_ID}/staff/", headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        staff_list = json.load(resp).get("data") or []
except Exception as e:
    staff_list = []
    print(f"    не вышло получить список сотрудников: {e}")

for person in staff_list:
    sid = person.get("id")
    url = f"{base}/book_times/{COMPANY_ID}/{sid}/{DATE}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            times = [d.get("time") for d in (json.load(resp).get("data") or [])]
    except Exception as e:
        times = f"ошибка: {e}"
    print(f"    {person.get('name')} (id={sid}): {times}")
