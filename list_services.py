#!/usr/bin/env python3
"""Выгружает список услуг филиала: id, название, категория, цена.

Нужен, чтобы задать боту процедуры: по id он добавит их в запись, по цене —
посчитает стоимость брони. Читает только, ничего не меняет.

Запуск: python3 list_services.py
"""
import getpass
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

COMPANY_ID = os.getenv("YCLIENTS_COMPANY_ID", "1395168")
ENV_PATH = Path(__file__).with_name(".env")


def env_value(name: str) -> str:
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

headers = {
    "Authorization": f"Bearer {partner}, User {user}" if user else f"Bearer {partner}",
    "Accept": "application/vnd.yclients.v2+json",
}

def fetch(path: str):
    request = urllib.request.Request(f"https://api.yclients.com/api/v1/{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response).get("data") or []
    except urllib.error.HTTPError as e:
        sys.exit(f"YCLIENTS ответил {e.code}: {e.read().decode(errors='replace')[:300]}")
    except urllib.error.URLError as e:
        sys.exit(f"Не достучались: {e.reason}")


services = fetch(f"company/{COMPANY_ID}/services/")
categories = {c.get("id"): c.get("title") for c in fetch(f"company/{COMPANY_ID}/service_categories/")}

print(f"Услуг всего: {len(services)}\n")
current = None
for service in sorted(services, key=lambda s: (s.get("category_id") or 0, s.get("title") or "")):
    category_id = service.get("category_id")
    if category_id != current:
        current = category_id
        print(f"\n=== категория {category_id}: {categories.get(category_id, '?')}")
        print(f"{'id':>10}  {'цена':>9}  {'онлайн':>6}  название")
        print("-" * 78)
    price = service.get("price_min")
    if price != service.get("price_max"):
        price = f"{service.get('price_min')}-{service.get('price_max')}"
    # У разных версий API признак онлайн-записи называется по-разному —
    # берём первый попавшийся, чтобы увидеть его глазами
    online = service.get("is_online")
    if online is None:
        online = service.get("online_invoicing_status")
    print(f"{service.get('id'):>10}  {str(price):>9}  {str(online):>6}  {service.get('title')}")

if services:
    print("\nПоля первой услуги — по ним видно, откуда брать цену:")
    print(json.dumps({k: v for k, v in services[0].items()
                      if not isinstance(v, (list, dict))}, ensure_ascii=False, indent=2)[:900])
