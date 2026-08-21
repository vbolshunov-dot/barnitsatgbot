#перед запуском бота, скрыть все API и ID
import html
import logging
import os
import re
import sys
import asyncio
import traceback
import httpx
from collections import Counter
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from telegram import (
    Update,
    BotCommand,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    PicklePersistence,
    ApplicationHandlerStop,
    filters,
)
from telegram.error import BadRequest

# Загружаем переменные из файла .env (он НЕ должен попадать в git)
load_dotenv()


def _int_env(name: str) -> int:
    """Читает целочисленную переменную окружения. Если её нет — вернёт 0,
    а check_config() найдёт это и остановит бот с понятной ошибкой."""
    value = os.getenv(name, "")
    return int(value) if value else 0


# Конфигурация бота и YCLIENTS — все секреты берутся из окружения
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
YCLIENTS_TOKEN = os.getenv("YCLIENTS_TOKEN", "")  # partner token
# Пользовательский токен YCLIENTS (User token). Некоторые методы API требуют
# заголовок вида "Bearer <partner_token>, User <user_token>".
# Если его нет / метод работает без него — оставьте пустым, ничего не сломается.
YCLIENTS_USER_TOKEN = os.getenv("YCLIENTS_USER_TOKEN", "")
YCLIENTS_COMPANY_ID = os.getenv("YCLIENTS_COMPANY_ID", "")

# Контакты менеджера для индивидуальных бронирований
MANAGER_PHONE = os.getenv("MANAGER_CONTACT_URL", "")
MANAGER_NAME = os.getenv("MANAGER_NAME", "")
# Ссылка на чат с менеджером — уходит в кнопки вида url=..., по ней открывается
# диалог в Telegram. Формат t.me/+<номер> работает и без username у менеджера.
MANAGER_TG_URL = os.getenv("MANAGER_TG_URL", "https://t.me/+79215530572")

# Чат, куда падают уведомления о новых бронях (рабочая группа администраторов).
# ID узнаётся командой /chatid, отправленной в саму группу. У групп он
# отрицательный, вида -1001234567890 — минус обязателен.
# Пусто — уведомления просто не отправляются, на бронирование это не влияет.
BOOKING_GROUP_CHAT_ID = os.getenv("BOOKING_GROUP_CHAT_ID", "")

# Личный чат администратора — туда падают технические ошибки бота. Именно в личку,
# а не в рабочую группу: тексты с трассировкой сотрудникам ни к чему.
# Узнаётся так же, командой /chatid, но отправленной боту в личном чате.
# Пусто — ошибки останутся только в логе.
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")
# Одна и та же ошибка часто повторяется десятками — например, когда отвалился
# YCLIENTS. Чтобы не завалить личку, повтор той же ошибки шлём не чаще чем раз
# в столько минут.
ERROR_NOTIFY_INTERVAL_MINUTES = 5

# Категория YCLIENTS, из которой берутся процедуры банного меню. Предлагаются
# только те её услуги, у которых включена онлайн-запись, — так список правится
# в кабинете, а не в коде: снял галку, и процедура пропала из бота.
PROCEDURES_CATEGORY_ID = int(os.getenv("PROCEDURES_CATEGORY_ID", "26014494"))
# Сколько ждём ответа YCLIENTS. Больше семи секунд ждать нет смысла: гость
# всё это время смотрит на «⏳», а запросов за одну бронь несколько подряд.
REQUEST_TIMEOUT = 7.0

# Насколько доверяем закэшированному прайсу. Цены меняются редко, а дёргать
# API на каждом шаге брони незачем.
SERVICES_CACHE_MINUTES = 10

# Папка с фотографиями банного меню. Файлы отправляются альбомом перед выбором
# процедур, порядок — по имени файла (1.jpg, 2.jpg, 3.jpg). Пусто или папки
# нет — шаг молча пропускается.
MENU_PHOTOS_DIR = Path(__file__).with_name("menu")

# Папка с листами прайса. Уходят по команде /price первым альбомом, следом за
# ними — фото банного меню. Порядок внутри папки — по имени файла.
PRICE_PHOTOS_DIR = Path(__file__).with_name("price")

# Файл для сохранения состояния диалогов между перезапусками бота
PERSISTENCE_FILE = os.getenv("PERSISTENCE_FILE", "bot_persistence.pickle")

# Часовой пояс филиала. Все даты и время в боте — местные, а сервер вполне может
# жить в UTC, поэтому расписание рассылок привязано к этой зоне явно.
LOCAL_TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))

# Когда уходят автоматические сообщения клиентам (по местному времени).
# Напоминание — накануне визита, просьба об отзыве — на следующее утро после него.
REMINDER_TIME = dt_time(12, 0)
FEEDBACK_TIME = dt_time(10, 0)

# Ключ в bot_data, под которым лежит список оформленных броней. Именно из него
# ежедневные задания берут, кому сегодня писать. Хранится в bot_data (а значит,
# переживает перезапуск через PicklePersistence), а не в виде отложенных job'ов:
# job'ы PTB не сохраняются и после деплоя все напоминания были бы потеряны.
BOOKINGS_KEY = "bookings"
# Через сколько дней после визита бронь выбрасывается из этого списка.
# Оба сообщения к тому моменту уже отправлены, держать дальше незачем.
BOOKINGS_KEEP_DAYS = 7


def check_config() -> None:
    """
    Проверяет, что все обязательные переменные окружения заданы.
    Лучше упасть сразу с понятной ошибкой, чем ловить 401/KeyError в середине работы.
    """
    required = {
        "BOT_TOKEN": BOT_TOKEN,
        "YCLIENTS_TOKEN": YCLIENTS_TOKEN,
        "YCLIENTS_COMPANY_ID": YCLIENTS_COMPANY_ID,
        "MANAGER_CONTACT_URL": MANAGER_PHONE,
        "MANAGER_NAME": MANAGER_NAME,
        "BIRCH_STAFF_ID": BATHS["birch"]["staff_id"],
        "PINE_STAFF_ID": BATHS["pine"]["staff_id"],
        "BIRCH_WEEKDAY_NO_PROC_SERVICE_ID": SERVICE_IDS["birch"]["weekday"]["no_proc"],
        "BIRCH_WEEKDAY_WITH_PROC_SERVICE_ID": SERVICE_IDS["birch"]["weekday"]["with_proc"],
        "BIRCH_WEEKEND_NO_PROC_SERVICE_ID": SERVICE_IDS["birch"]["weekend"]["no_proc"],
        "BIRCH_WEEKEND_WITH_PROC_SERVICE_ID": SERVICE_IDS["birch"]["weekend"]["with_proc"],
        "PINE_WEEKDAY_NO_PROC_SERVICE_ID": SERVICE_IDS["pine"]["weekday"]["no_proc"],
        "PINE_WEEKDAY_WITH_PROC_SERVICE_ID": SERVICE_IDS["pine"]["weekday"]["with_proc"],
        "PINE_WEEKEND_NO_PROC_SERVICE_ID": SERVICE_IDS["pine"]["weekend"]["no_proc"],
        "PINE_WEEKEND_WITH_PROC_SERVICE_ID": SERVICE_IDS["pine"]["weekend"]["with_proc"],
    }
    if not YCLIENTS_USER_TOKEN:
        print(
            "ВНИМАНИЕ: не задан YCLIENTS_USER_TOKEN. Без него YCLIENTS не даёт "
            "искать клиентов по базе, и те, кто уже есть в базе салона, не смогут "
            "зарегистрироваться в боте.",
            file=sys.stderr,
        )

    if not BOOKING_GROUP_CHAT_ID:
        print(
            "ВНИМАНИЕ: не задан BOOKING_GROUP_CHAT_ID — уведомления о новых бронях "
            "в рабочую группу отправляться не будут. Узнать ID: добавьте бота в "
            "группу и отправьте там команду /chatid",
            file=sys.stderr,
        )

    if not ADMIN_CHAT_ID:
        print(
            "ВНИМАНИЕ: не задан ADMIN_CHAT_ID — ошибки бота будут писаться только "
            "в лог, в личку они не придут. Узнать ID: отправьте боту /chatid "
            "в личном чате.",
            file=sys.stderr,
        )

    missing = [name for name, value in required.items() if not value]
    if missing:
        print(
            "ОШИБКА: не заданы переменные окружения: " + ", ".join(missing) +
            "\nСоздайте файл .env рядом с этим скриптом (образец — .env.example)",
            file=sys.stderr,
        )
        sys.exit(1)


# Один HTTP-клиент на весь бот. Раньше клиент создавался под каждый запрос, и
# каждый раз заново шло TLS-рукопожатие с api.yclients.com — по 200-400 мс на
# ровном месте, а за одну бронь запросов пять-шесть. Общий клиент держит
# соединение открытым и переиспользует его.
HTTP: dict = {"client": None}


def http_client() -> httpx.AsyncClient:
    """Отдаёт общий HTTP-клиент, при необходимости создавая его.

    Создаётся лениво, а не на старте, чтобы отдельные скрипты рядом с ботом
    могли импортировать эти функции, не поднимая всё приложение целиком.
    """
    client = HTTP.get("client")
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        HTTP["client"] = client
    return client


async def close_http_client() -> None:
    """Закрывает общий клиент при остановке бота, чтобы не оставлять соединения."""
    client = HTTP.get("client")
    if client is not None and not client.is_closed:
        await client.aclose()
        logger.info("HTTP-клиент закрыт")


def yclients_auth_header() -> str:
    """Собирает значение заголовка Authorization для YCLIENTS с учётом User-токена"""
    if YCLIENTS_USER_TOKEN:
        return f"Bearer {YCLIENTS_TOKEN}, User {YCLIENTS_USER_TOKEN}"
    return f"Bearer {YCLIENTS_TOKEN}"

#конфигурация бань: staff_id для каждой бани, лимиты гостей и времени
BATHS = {
    "birch": {
        "name": "Берёзовая",
        "staff_id": _int_env("BIRCH_STAFF_ID"),  # ID сотрудника в YCLIENTS для березовой бани
        "max_guests": 4,  # максимальное количество гостей
        "min_hours": 3,  # минимальное время аренды в часах
        "desc": "до 4 гостей, от 3 часов"  # описание для отображения пользователю
    },
    "pine": {
        "name": "Хвойная",
        "staff_id": _int_env("PINE_STAFF_ID"),  # ID сотрудника в YCLIENTS для хвойной бани
        "max_guests": 8,  # максимальное количество гостей
        "min_hours": 4,  # минимальное время аренды в часах
        "desc": "до 8 гостей, от 4 часов"  # описание для отображения пользователю
    }
}

# матрица service_id: баня -> тип дня -> с/без процедур
# service_id берутся из YCLIENTS, каждая комбинация = отдельная услуга
SERVICE_IDS = {
    "birch": {
        "weekday": {
            "no_proc": _int_env("BIRCH_WEEKDAY_NO_PROC_SERVICE_ID"),  # берёзовая 3 часа будни без процедур
            "with_proc": _int_env("BIRCH_WEEKDAY_WITH_PROC_SERVICE_ID"),  # берёзовая будни с процедурами
        },
        "weekend": {
            "no_proc": _int_env("BIRCH_WEEKEND_NO_PROC_SERVICE_ID"),  # берёзовая выходной без процедур
            "with_proc": _int_env("BIRCH_WEEKEND_WITH_PROC_SERVICE_ID"),  # берёзовая выходные с процедурами
        }
    },
    "pine": {
        "weekday": {
            "no_proc": _int_env("PINE_WEEKDAY_NO_PROC_SERVICE_ID"),  # хвойная 4 часа будни без процедур
            "with_proc": _int_env("PINE_WEEKDAY_WITH_PROC_SERVICE_ID"),  # хвойная будний с процедурами
        },
        "weekend": {
            "no_proc": _int_env("PINE_WEEKEND_NO_PROC_SERVICE_ID"),  # хвойная выходные без процедур
            "with_proc": _int_env("PINE_WEEKEND_WITH_PROC_SERVICE_ID"),  # хвойная выходной с процедурами
        }
    }
}

# Длительность сеанса для каждой услуги, В ЧАСАХ.
# ВАЖНО: значения должны совпадать со столбцом «Длительность» у услуги в YCLIENTS
# (Настройки → Услуги). Если не совпадут — запись либо не создастся,
# либо займёт в журнале неверный интервал.
SERVICE_DURATIONS = {
    "birch": {
        "weekday": {
            "no_proc": 3,    # берёзовая будни без процедур — 03 ч 00 м
            "with_proc": 3   # берёзовая будни с процедурами — 03 ч 00 м
        },
        "weekend": {
            "no_proc": 3,    # берёзовая выходные без процедур — 03 ч 00 м
            "with_proc": 3   # берёзовая выходные с процедурами — 03 ч 00 м
        }
    },
    "pine": {
        "weekday": {
            "no_proc": 4,    # ПРОВЕРИТЬ длительность в YCLIENTS
            "with_proc": 4   # ПРОВЕРИТЬ длительность в YCLIENTS
        },
        "weekend": {
            "no_proc": 4,    # ПРОВЕРИТЬ длительность в YCLIENTS
            "with_proc": 4   # ПРОВЕРИТЬ длительность в YCLIENTS
        }
    }
}

# Фиксированное начало сеансов в каждой бане.
# Клиенту предлагаются только эти варианты, а не вся сетка YCLIENTS: баня топится
# под конкретный заход, между заходами нужен перерыв на уборку.
# Конец сеанса не задаётся здесь, а считается из SERVICE_DURATIONS, чтобы кнопка
# показывала ровно ту длительность, которая уйдёт в запись.
#   Берёзовая (3 ч): 10:00-13:00, 14:00-17:00, 18:00-21:00
#   Хвойная  (4 ч): 10:00-14:00, 16:00-20:00
# Разбивка по типу дня оставлена на будущее: если график заходов в будни и
# выходные разойдётся, менять придётся только эту таблицу.
SEANCE_STARTS = {
    "birch": {
        "weekday": ["10:00", "14:00", "18:00"],
        "weekend": ["10:00", "14:00", "18:00"],
    },
    "pine": {
        "weekday": ["10:00", "16:00"],
        "weekend": ["10:00", "16:00"],
    },
}

# На сколько дней вперёд предлагать даты и по сколько кнопок ставить в ряд.
# Ряд из трёх коротких кнопок даёт втрое больше дат на том же экране.
DAYS_AHEAD = 21
DATE_COLUMNS = 3

# настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# состояния для сonversationHandler регистрации
NAME, PHONE, CONFIRM = range(3)
# состояния для сonversationHandler бронирования
CHOOSE_BATH, CHOOSE_DAY_TYPE, WITH_PROCEDURES, GUEST_COUNT, PICK_PROCEDURES, BOOK_DATE, BOOK_TIME = range(3, 10)

# ключи временных данных бронирования (сбрасываются при /cancel и /start)
BOOKING_KEYS = ("bath_id", "day_type", "with_procedures", "guest_count", "book_date", "procedures")

# регулярка для валидации российского номера телефона
PHONE_REGEX = re.compile(r'^\+7\d{10}$')

# Ссылки на ConversationHandler'ы для force_* команд.
# Хранятся здесь, а не в bot_data: bot_data пиклится персистентностью,
# а ConversationHandler не сериализуется.
HANDLERS: dict = {}


def book_button(text: str = "🌿 Забронировать баню") -> InlineKeyboardButton:
    """Кнопка вместо подсказки «/book» — команду не надо набирать руками."""
    return InlineKeyboardButton(text, callback_data="go_book")


def start_button(text: str = "📝 Зарегистрироваться") -> InlineKeyboardButton:
    """Кнопка вместо подсказки «/start»."""
    return InlineKeyboardButton(text, callback_data="go_start")


def manager_button(text: str = "📞 Написать менеджеру") -> InlineKeyboardButton:
    """Кнопка-ссылка на чат с менеджером. Нажатие открывает диалог в Telegram
    и не присылает боту callback — обработчик ей не нужен."""
    return InlineKeyboardButton(text, url=MANAGER_TG_URL)


def is_weekend(date_obj: datetime) -> bool:
    """Проверяет является ли дата выходным днем (суббота или воскресенье)"""
    # Пн=0, Вт=1, Ср=2, Чт=3, Пт=4, Сб=5, Вс=6
    return date_obj.weekday() >= 5


def get_service_id(bath_id: str, day_type: str, with_proc: bool) -> int:
    """
    Определяет service_id на основе выбранных параметров

    Args:
        bath_id: ID бани (birch/pine)
        day_type: Тип дня (weekday/weekend)
        with_proc: Нужны ли процедуры (True/False)

    Returns:
        int: service_id из YCLIENTS
    """
    proc_key = "with_proc" if with_proc else "no_proc"
    service_id = SERVICE_IDS[bath_id][day_type][proc_key]
    logger.info(f"Определён service_id={service_id} для бани={bath_id}, день={day_type}, процедуры={with_proc}")
    return service_id


def get_seance_length(bath_id: str, day_type: str, with_proc: bool) -> int:
    """
    Возвращает длительность сеанса В СЕКУНДАХ — YCLIENTS ожидает именно секунды
    в параметре seance_length при создании записи.

    Args:
        bath_id: ID бани (birch/pine)
        day_type: Тип дня (weekday/weekend)
        with_proc: Нужны ли процедуры (True/False)

    Returns:
        int: длительность сеанса в секундах
    """
    proc_key = "with_proc" if with_proc else "no_proc"
    hours = SERVICE_DURATIONS[bath_id][day_type][proc_key]
    seconds = hours * 3600
    logger.info(f"Длительность сеанса: {hours} ч ({seconds} сек)")
    return seconds


WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def format_date_ru(date_iso: str) -> str:
    """2026-08-26 -> '26.08.2026, среда'. Для администратора день недели
    понятнее, чем «будни»: сразу видно, о каком дне речь."""
    day = datetime.strptime(date_iso, "%Y-%m-%d")
    return f"{day.strftime('%d.%m.%Y')}, {WEEKDAYS_RU[day.weekday()]}"


def money(value) -> str:
    """12000 -> '12 000 ₽'. Неразрывный пробел, чтобы цена не рвалась на строки."""
    return f"{int(value):,}".replace(",", " ") + " ₽"


# Прайс целиком, одним запросом на все услуги: {id: {"title": ..., "price": ...}}
SERVICES_CACHE: dict = {"at": None, "index": {}}


async def get_services_index() -> dict:
    """
    Возвращает справочник услуг филиала с ценами, обновляя его не чаще раза
    в SERVICES_CACHE_MINUTES минут.

    Цены берём из YCLIENTS, а не из кода: поменял прайс в кабинете — бот
    подхватил сам, деплой не нужен.

    Returns:
        dict: {service_id: {"title", "price", "category_id", "is_online"}}
    """
    now = datetime.now()
    cached_at = SERVICES_CACHE["at"]
    if cached_at and (now - cached_at) < timedelta(minutes=SERVICES_CACHE_MINUTES):
        return SERVICES_CACHE["index"]

    url = f"https://api.yclients.com/api/v1/company/{YCLIENTS_COMPANY_ID}/services/"
    headers = {"Authorization": yclients_auth_header(), "Accept": "application/vnd.yclients.v2+json"}

    try:
        client = http_client()
        response = await client.get(url, headers=headers)
        if response.status_code != 200:
            logger.error(f"services вернул {response.status_code}: {response.text}")
            return SERVICES_CACHE["index"]
        services = response.json().get('data') or []
    except Exception as e:
        logger.error(f"Ошибка получения услуг: {e}")
        # Отдаём то, что было: устаревшая цена лучше, чем сорванная бронь
        return SERVICES_CACHE["index"]

    index = {
        int(service["id"]): {
            "title": service.get("title") or "",
            "price": service.get("price_min") or 0,
            "category_id": service.get("category_id"),
            "is_online": bool(service.get("is_online")),
        }
        for service in services if service.get("id")
    }
    SERVICES_CACHE.update(at=now, index=index)
    logger.info(f"Прайс обновлён: {len(index)} услуг")
    return index


async def get_procedures() -> list:
    """
    Процедуры банного меню, доступные для онлайн-записи.

    Returns:
        list: [{"id", "title", "price"}], отсортированы по цене — дешёвые сверху
    """
    index = await get_services_index()
    procedures = [
        {"id": service_id, "title": item["title"], "price": item["price"]}
        for service_id, item in index.items()
        if item["category_id"] == PROCEDURES_CATEGORY_ID and item["is_online"]
    ]
    procedures.sort(key=lambda p: (p["price"], p["title"]))
    logger.info(f"Процедур доступно: {len(procedures)}")
    return procedures


def price_breakdown(bath_title: str, bath_price: int, chosen: list, index: dict) -> tuple:
    """
    Собирает расшифровку стоимости брони.

    Args:
        chosen: id выбранных процедур, повторы значимы (двое взяли одно и то же)
        index: справочник из get_services_index()

    Returns:
        tuple: (строки расшифровки, итоговая сумма)
    """
    lines = [f"🌿 {bath_title} — {money(bath_price)}"]
    total = int(bath_price)

    for service_id, count in Counter(chosen).items():
        item = index.get(service_id) or {}
        price = int(item.get("price") or 0)
        title = item.get("title") or f"услуга {service_id}"
        suffix = f" × {count}" if count > 1 else ""
        lines.append(f"💆 {title}{suffix} — {money(price * count)}")
        total += price * count

    return lines, total


def seance_end(start: str, bath_id: str, day_type: str, with_proc: bool) -> str:
    """Считает конец сеанса из длительности услуги: "14:00" -> "17:00".
    Отдельной таблицы с концами заходов нет намеренно — иначе она разъедется
    с SERVICE_DURATIONS, и клиент увидит одно, а в журнал уйдёт другое."""
    hours = get_seance_length(bath_id, day_type, with_proc) // 3600
    return (datetime.strptime(start, "%H:%M") + timedelta(hours=hours)).strftime("%H:%M")


def _phone_digits(phone: str) -> str:
    """Оставляет от номера только цифры: +7 (999) 123-45-67 -> 79991234567.
    YCLIENTS хранит телефон без плюса, поэтому искать надо по цифрам."""
    return re.sub(r'\D', '', str(phone))


async def find_client_by_phone(client: httpx.AsyncClient, phone: str, headers: dict) -> int:
    """
    Ищет клиента в базе YCLIENTS по номеру телефона.

    Нужно для тех, кто уже есть в базе салона (записывался по телефону, заведён
    администратором), но в боте ещё не регистрировался: создать его повторно
    YCLIENTS не даст, а работать он должен под своим существующим id.

    Returns:
        int: id клиента в YCLIENTS или 0, если не нашли
    """
    digits = _phone_digits(phone)

    # Основной способ — поиск по базе клиентов. Требует User-токен.
    search_url = f"https://api.yclients.com/api/v1/company/{YCLIENTS_COMPANY_ID}/clients/search"
    payload = {
        "fields": ["id", "name", "phone"],
        "filters": [{"type": "quick_search", "state": {"value": digits}}],
        "page": 1,
        "page_size": 10,
    }
    try:
        resp = await client.post(search_url, json=payload, headers=headers)
        if resp.status_code == 200:
            for item in resp.json().get("data") or []:
                if _phone_digits(item.get("phone", "")) == digits:
                    logger.info(f"Клиент найден в базе YCLIENTS: id={item['id']}")
                    return int(item["id"])
            logger.info(f"Поиск по базе не дал совпадений для {digits}")
        else:
            logger.error(f"Yclients clients/search error: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"Yclients clients/search exception: {e}")

    # Запасной способ — старый GET-фильтр по списку клиентов.
    list_url = f"https://api.yclients.com/api/v1/clients/{YCLIENTS_COMPANY_ID}"
    try:
        resp = await client.get(list_url, headers=headers, params={"phone": digits})
        if resp.status_code == 200:
            for item in resp.json().get("data") or []:
                if _phone_digits(item.get("phone", "")) == digits:
                    logger.info(f"Клиент найден через GET /clients: id={item['id']}")
                    return int(item["id"])
        else:
            logger.error(f"Yclients GET clients error: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"Yclients GET clients exception: {e}")

    return 0


async def register_in_yclients(name: str, phone: str, telegram_id: int) -> tuple[bool, int]:
    """
    Находит клиента в базе YCLIENTS по телефону, а если его там нет — создаёт.

    Сначала ищем, потом создаём: клиент может уже быть в базе салона, хотя в боте
    он впервые. Повторное создание YCLIENTS отклонит, и человек упрётся в ошибку.

    Args:
        name: Имя и фамилия клиента
        phone: Номер телефона в формате +79991234567
        telegram_id: Telegram ID пользователя для связи

    Returns:
        tuple: (успех, yclients_id)
    """
    url = f"https://api.yclients.com/api/v1/clients/{YCLIENTS_COMPANY_ID}"
    headers = {
        "Authorization": yclients_auth_header(),
        "Accept": "application/vnd.yclients.v2+json",
        "Content-Type": "application/json"
    }
    payload = {"name": name, "phone": phone, "comment": f"Telegram ID: {telegram_id}"}

    try:
        client = http_client()
        existing_id = await find_client_by_phone(client, phone, headers)
        if existing_id:
            logger.info(f"Клиент {phone} уже есть в базе, используем id={existing_id}")
            return True, existing_id

        logger.info(f"Отправляем запрос регистрации в YCLIENTS: {name}, {phone}")
        response = await client.post(url, json=payload, headers=headers)

        if response.status_code in (200, 201):
            data = response.json()
            client_id = (data.get('data') or {}).get('id')
            if client_id:
                logger.info(f"Клиент успешно зарегистрирован в YCLIENTS: id={client_id}")
                return True, int(client_id)

        # Создать не вышло. Частый случай — клиент всё-таки есть в базе,
        # но поиск его не увидел (другой формат номера, права токена).
        # Пробуем найти ещё раз, прежде чем сдаваться.
        logger.error(f"Yclients register error: {response.status_code} {response.text}")
        existing_id = await find_client_by_phone(client, phone, headers)
        if existing_id:
            logger.info(f"Клиент найден после неудачного создания: id={existing_id}")
            return True, existing_id

        return False, 0
    except Exception as e:
        logger.error(f"Yclients exception: {e}")
        return False, 0


def _parse_local(value: str) -> datetime:
    """YCLIENTS отдаёт время вида 2026-08-26T10:00:00+03:00. Часовой пояс
    у филиала один и тот же, поэтому берём первые 19 символов и работаем
    в местном времени — так проще и не надо тащить tz-арифметику."""
    return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")


async def get_working_intervals(staff_id: int, date: str) -> list:
    """
    Возвращает рабочие интервалы бани на дату по графику YCLIENTS.

    Returns:
        list: пары (начало, конец) как datetime; пустой список — выходной
              или запрос не удался
    """
    url = f"https://api.yclients.com/api/v1/schedule/{YCLIENTS_COMPANY_ID}/{staff_id}/{date}/{date}"
    headers = {"Authorization": yclients_auth_header(), "Accept": "application/vnd.yclients.v2+json"}

    try:
        client = http_client()
        response = await client.get(url, headers=headers)
        if response.status_code != 200:
            logger.error(f"schedule вернул {response.status_code}: {response.text}")
            return []
        days = response.json().get('data') or []
    except Exception as e:
        logger.error(f"Ошибка получения графика: {e}")
        return []

    intervals = []
    for day in days:
        if not day.get('is_working'):
            continue
        for slot in day.get('slots') or []:
            start = datetime.strptime(f"{date} {slot['from']}", "%Y-%m-%d %H:%M")
            end = datetime.strptime(f"{date} {slot['to']}", "%Y-%m-%d %H:%M")
            intervals.append((start, end))

    logger.info(f"График бани {staff_id} на {date}: {[(s.strftime('%H:%M'), e.strftime('%H:%M')) for s, e in intervals]}")
    return intervals


async def get_busy_intervals(staff_id: int, date: str) -> list:
    """
    Возвращает занятые интервалы бани на дату по журналу записей.

    Returns:
        tuple: (успех, список пар (начало, конец) как datetime). Успех False
               означает, что журнал прочитать не удалось — предлагать заходы
               в этом случае нельзя, иначе можно посадить двоих на один сеанс.
    """
    url = f"https://api.yclients.com/api/v1/records/{YCLIENTS_COMPANY_ID}"
    headers = {"Authorization": yclients_auth_header(), "Accept": "application/vnd.yclients.v2+json"}
    params = {"staff_id": staff_id, "start_date": date, "end_date": date, "count": 200}

    try:
        client = http_client()
        response = await client.get(url, headers=headers, params=params)
        if response.status_code != 200:
            logger.error(f"records вернул {response.status_code}: {response.text}")
            return False, []
        records = response.json().get('data') or []
    except Exception as e:
        logger.error(f"Ошибка получения записей: {e}")
        return False, []

    busy = []
    for record in records:
        if record.get('deleted'):
            continue
        start = _parse_local(record['datetime'])
        busy.append((start, start + timedelta(seconds=int(record.get('seance_length') or 0))))

    logger.info(f"Занято в бане {staff_id} на {date}: {[(s.strftime('%H:%M'), e.strftime('%H:%M')) for s, e in busy]}")
    return True, busy


async def get_free_seances(bath_id: str, day_type: str, with_proc: bool, date: str) -> list:
    """
    Считает, какие фиксированные заходы свободны, НЕ спрашивая book_times.

    Клиентский метод book_times у этого филиала отдаёт сетку, которая не
    совпадает ни с графиком бани, ни с настройками услуги: он молча срезает
    первый час рабочего дня. Из-за этого утренний заход в 10:00 не появлялся
    в боте, хотя баня свободна, а административный метод записи такую бронь
    спокойно создаёт. Поэтому занятость считаем сами: график минус журнал.

    Returns:
        list: времена начала свободных заходов, например ["10:00", "18:00"]
    """
    staff_id = int(BATHS[bath_id]['staff_id'])
    hours = get_seance_length(bath_id, day_type, with_proc) // 3600

    # График и журнал друг от друга не зависят — спрашиваем оба разом, а не по
    # очереди: шаг «Ищу свободное время» становится вдвое короче. В нерабочий
    # день уходит один лишний запрос, но это редкий случай и он того стоит.
    working, (ok, busy) = await asyncio.gather(
        get_working_intervals(staff_id, date),
        get_busy_intervals(staff_id, date),
    )
    if not working:
        logger.info(f"Баня {bath_id} на {date} не работает или график не получен")
        return []
    if not ok:
        return []

    free = []
    for start_str in SEANCE_STARTS[bath_id][day_type]:
        start = datetime.strptime(f"{date} {start_str}", "%Y-%m-%d %H:%M")
        end = start + timedelta(hours=hours)
        if not any(w_start <= start and end <= w_end for w_start, w_end in working):
            continue
        # Пересечение интервалов: заход занят, если он с чем-то накладывается.
        # Касание встык (10:00-13:00 и 13:00-16:00) пересечением не считается.
        if any(start < b_end and b_start < end for b_start, b_end in busy):
            continue
        free.append(start_str)

    logger.info(f"Свободные заходы {bath_id}/{day_type} на {date}: {free}")
    return free


async def create_booking(yclients_id: int, staff_id: int, services: list, datetime_str: str, comment: str,
                         seance_length: int) -> bool:
    """
    Создаёт запись на услугу в YCLIENTS

    Args:
        yclients_id: ID клиента в YCLIENTS
        staff_id: ID сотрудника (бани)
        services: услуги записи в виде [{"id": ..., "amount": ...}] — аренда бани
                  плюс выбранные процедуры
        datetime_str: Дата и время в формате "YYYY-MM-DD HH:MM:SS"
        comment: Комментарий к записи
        seance_length: Длительность сеанса В СЕКУНДАХ

    Returns:
        bool: True если запись создана успешно
    """
    url = f"https://api.yclients.com/api/v1/records/{YCLIENTS_COMPANY_ID}"
    headers = {
        "Authorization": yclients_auth_header(),
        "Accept": "application/vnd.yclients.v2+json",
        "Content-Type": "application/json"
    }
    payload = {
        "staff_id": staff_id,
        "services": services,
        "client": {"id": yclients_id},
        "datetime": datetime_str,
        "seance_length": seance_length,
        "comment": comment
    }

    try:
        client = http_client()
        logger.info(
            f"Создаём запись в YCLIENTS: client_id={yclients_id}, услуги={services}, datetime={datetime_str}")
        response = await client.post(url, json=payload, headers=headers)
        if response.status_code == 201:
            logger.info("Запись успешно создана в YCLIENTS")
            return True
        logger.error(f"Ошибка создания записи: {response.status_code} {response.text}")
        return False
    except Exception as e:
        logger.error(f"Exception создания записи: {e}")
        return False


async def notify_group_about_booking(context: ContextTypes.DEFAULT_TYPE, user, details: dict) -> None:
    """
    Присылает в рабочую группу карточку новой брони.

    Ошибки отправки только логируются: для клиента бронь уже создана, и падать
    из-за того, что бота выкинули из группы, бот не должен.

    Args:
        user: telegram-пользователь, оформивший бронь
        details: поля брони для карточки (см. вызов в book_time)
    """
    if not BOOKING_GROUP_CHAT_ID:
        logger.info("BOOKING_GROUP_CHAT_ID не задан — уведомление в группу не отправляем")
        return

    # Имя и телефон приходят от пользователя, в HTML их нужно экранировать.
    # mention_html() экранирует себя сам.
    account = user.mention_html()
    if user.username:
        account += f" (@{user.username})"

    text = (
        "🆕 <b>Новая бронь</b>\n\n"
        f"👤 {html.escape(details['name'])}\n"
        f"📱 {html.escape(details['phone'])}\n"
        f"✈️ {account}\n"
        f"🆔 <code>{user.id}</code>\n\n"
        f"🌿 Баня: {details['bath']}\n"
        f"📅 Дата: {format_date_ru(details['date'])}\n"
        f"⏰ Сеанс: {details['start']} – {details['end']}\n"
        f"💆 Процедуры: {details['proc_text']}\n"
        f"🧖 Банное меню: {html.escape(str(details.get('procedures', 'нет')))}\n"
        f"👥 Гостей: {details['guests']}\n"
        f"💰 Итого: {money(details.get('total', 0))}"
    )

    try:
        await context.bot.send_message(
            chat_id=BOOKING_GROUP_CHAT_ID, text=text, parse_mode="HTML"
        )
        logger.info(f"Уведомление о брони отправлено в группу {BOOKING_GROUP_CHAT_ID}")
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление в группу {BOOKING_GROUP_CHAT_ID}: {e}")


def _today_local() -> date:
    """Сегодняшняя дата по часовому поясу филиала, без времени.

    Именно по поясу филиала, а не сервера: хостинг стоит в другой зоне и его
    дата переключается в другой момент, чем в бане."""
    return datetime.now(LOCAL_TZ).date()


def booking_card(booking: dict) -> str:
    """Карточка брони для писем клиенту: что, когда и на сколько человек."""
    return (
        f"\U0001F33F Баня: {booking['bath']}\n"
        f"\U0001F4C5 Дата: {format_date_ru(booking['date'])}\n"
        f"\u23F0 Сеанс: {booking['start']} – {booking['end']}\n"
        f"\U0001F465 Гостей: {booking['guests']}\n"
        f"\U0001F9D6 Банное меню: {booking['procedures']}"
    )


def remember_booking(context: ContextTypes.DEFAULT_TYPE, chat_id: int, details: dict) -> None:
    """Запоминает бронь, чтобы накануне визита прислать напоминание, а наутро
    после него — попросить об отзыве.

    Args:
        chat_id: личный чат клиента с ботом, туда уйдут оба сообщения
        details: те же поля брони, что идут в карточку для группы
    """
    booking = {
        "chat_id": chat_id,
        "name": details.get("name", ""),
        "bath": details["bath"],
        "date": details["date"],
        "start": details["start"],
        "end": details["end"],
        "guests": details["guests"],
        "procedures": details.get("procedures", "нет"),
        "reminded": False,
        "feedback_sent": False,
    }
    context.bot_data.setdefault(BOOKINGS_KEY, []).append(booking)
    logger.info(f"Бронь на {booking['date']} поставлена в очередь напоминаний (чат {chat_id})")


def forget_old_bookings(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Выбрасывает брони, которые давно прошли, — список не должен расти вечно."""
    bookings = context.bot_data.get(BOOKINGS_KEY)
    if not bookings:
        return
    edge = (_today_local() - timedelta(days=BOOKINGS_KEEP_DAYS)).strftime("%Y-%m-%d")
    kept = [b for b in bookings if b.get("date", "") >= edge]
    if len(kept) != len(bookings):
        logger.info(f"Из очереди напоминаний убрано старых броней: {len(bookings) - len(kept)}")
        context.bot_data[BOOKINGS_KEY] = kept


async def _send_to_client(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, markup) -> bool:
    """Отправляет сообщение клиенту. Возвращает False, если не дошло.

    Ошибку только логируем: клиент мог заблокировать бота, и падать из-за
    одного такого рассылка не должна — остальным написать всё равно нужно.
    """
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
        return True
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение клиенту {chat_id}: {e}")
        return False


async def send_day_before_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Напоминание накануне визита — всем, у кого баня забронирована на завтра.

    Если бронь оформили в тот же день позже REMINDER_TIME, напоминание не уйдёт:
    задание за сегодня уже отработало, а у клиента и так свежее подтверждение.
    """
    target = (_today_local() + timedelta(days=1)).strftime("%Y-%m-%d")
    markup = InlineKeyboardMarkup([[manager_button("\U0001F4DE Связаться с менеджером")]])

    for booking in context.bot_data.get(BOOKINGS_KEY, []):
        if booking.get("reminded") or booking.get("date") != target:
            continue
        name = booking.get("name") or "друзья"
        text = (
            f"Доброго времени суток, {name}!\n\n"
            f"На завтра у вас забронирована баня:\n"
            f"{booking_card(booking)}\n\n"
            f"Не забудьте взять с собой купальники и тапочки.\n"
            f"До встречи в доме тепла и отдыха «Барница»!"
        )
        if await _send_to_client(context, booking["chat_id"], text, markup):
            booking["reminded"] = True
            logger.info(f"Напоминание о завтрашней бане отправлено в чат {booking['chat_id']}")

    forget_old_bookings(context)


async def send_feedback_requests(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Просьба об отзыве — наутро после визита."""
    target = (_today_local() - timedelta(days=1)).strftime("%Y-%m-%d")
    markup = InlineKeyboardMarkup([
        [manager_button("\U0001F4AC Поделиться впечатлениями")],
        [book_button("\U0001F33F Забронировать снова")],
    ])

    for booking in context.bot_data.get(BOOKINGS_KEY, []):
        if booking.get("feedback_sent") or booking.get("date") != target:
            continue
        text = (
            "Доброе утро! Надеемся, что вчера Вам удалось отдохнуть и восстановиться "
            "в нашем банном дворе. Для нас очень важна обратная связь, поэтому просим "
            "вас поделиться впечатлениями у нас в гостях. Благодарим Вас и ждём в гости снова!"
        )
        if await _send_to_client(context, booking["chat_id"], text, markup):
            booking["feedback_sent"] = True
            logger.info(f"Просьба об отзыве отправлена в чат {booking['chat_id']}")

    forget_old_bookings(context)


def clear_booking_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Удаляет временные данные бронирования, не трогая регистрацию."""
    for key in BOOKING_KEYS:
        context.user_data.pop(key, None)


def end_all_conversations(update: Update, application: Application) -> None:
    """Сбрасывает активные диалоги ConversationHandler для пользователя."""
    for handlers in application.handlers.values():
        for handler in handlers:
            if isinstance(handler, ConversationHandler):
                key = handler._get_key(update)
                handler._conversations.pop(key, None)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик команды /start - начало регистрации"""
    clear_booking_data(context)
    user = update.effective_user
    logger.info(f"Пользователь {user.id} начал регистрацию")
    # effective_message, а не message: сюда попадают и нажатия кнопки
    # «Зарегистрироваться», у которых update.message пустой.
    await update.effective_message.reply_html(
        f"Привет, {user.mention_html()}! Добро пожаловать в наш банный комплекс 🧖‍♂️\n\n"
        f"Для бронирования нужно зарегистрироваться.\n\n"
        f"Продолжая, вы даёте согласие на обработку персональных данных "
        f"(имя, телефон) в целях бронирования услуг.\n\n "
        f"Как вас зовут? Напишите имя и фамилию.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return NAME


async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает имя пользователя и запрашивает телефон"""
    name = update.message.text.strip()
    if len(name) < 2:
        logger.warning(f"Пользователь {update.effective_user.id} ввёл слишком короткое имя: {name}")
        await update.message.reply_text("Имя слишком короткое. Введите имя и фамилию:")
        return NAME

    context.user_data['name'] = name
    logger.info(f"Пользователь {update.effective_user.id} ввёл имя: {name}")
    keyboard = [["Отменить регистрацию"]]
    await update.message.reply_text(
        f"Отлично, {name}!\nТеперь отправьте номер телефона в формате +79991234567",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return PHONE


async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает и валидирует номер телефона"""
    phone = update.message.text.strip()
    if phone == "Отменить регистрацию":
        logger.info(f"Пользователь {update.effective_user.id} отменил регистрацию")
        return await cancel(update, context)

    if not PHONE_REGEX.match(phone):
        logger.warning(f"Пользователь {update.effective_user.id} ввёл неверный формат телефона: {phone}")
        await update.message.reply_text(
            "Неверный формат 😕\nНомер должен быть +79991234567\nПопробуйте еще раз:"
        )
        return PHONE

    context.user_data['phone'] = phone
    name = context.user_data['name']
    logger.info(f"Пользователь {update.effective_user.id} ввёл телефон: {phone}")

    keyboard = [["✅ Всё верно", "❌ Изменить"]]
    await update.message.reply_text(
        f"Проверьте данные:\n\n👤 Имя: {name}\n📱 Телефон: {phone}\n\nВсё правильно?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return CONFIRM


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Подтверждает данные и регистрирует в YCLIENTS"""
    if update.message.text == "❌ Изменить":
        logger.info(f"Пользователь {update.effective_user.id} решил изменить данные")
        await update.message.reply_text("Хорошо, начнём заново. Как вас зовут?", reply_markup=ReplyKeyboardRemove())
        return NAME

    await update.message.reply_text("Регистрирую вас... ⏳", reply_markup=ReplyKeyboardRemove())

    success, yclient_id = await register_in_yclients(
        name=context.user_data['name'],
        phone=context.user_data['phone'],
        telegram_id=update.effective_user.id
    )

    if success:
        context.user_data['yclients_id'] = yclient_id
        logger.info(f"Пользователь {update.effective_user.id} успешно зарегистрирован, yclients_id={yclient_id}")
        await update.message.reply_text(
            "Готово! Вы зарегистрированы ✅",
            reply_markup=InlineKeyboardMarkup([[book_button()]]),
        )
    else:
        logger.error(f"Ошибка регистрации пользователя {update.effective_user.id}")
        await update.message.reply_text(
            "Не получилось вас зарегистрировать 😔\n\n"
            "Попробуйте ещё раз или напишите менеджеру:\n"
            f"👤 {MANAGER_NAME}\n📱 {MANAGER_PHONE}",
            reply_markup=InlineKeyboardMarkup([
                [start_button("🔄 Попробовать снова")],
                [manager_button()],
            ]),
        )

    return ConversationHandler.END


async def show_bath_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает клавиатуру выбора бани"""
    keyboard = []
    for bath_id, bath_data in BATHS.items():
        text = f"🌿 {bath_data['name']} — {bath_data['desc']}"
        keyboard.append([InlineKeyboardButton(text, callback_data=f"bath_{bath_id}")])

    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="book_cancel")])
    markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_text("Выберите баню:", reply_markup=markup)
    else:
        await update.callback_query.edit_message_text("Выберите баню:", reply_markup=markup)


async def book_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик команды /book - начало бронирования"""
    if 'yclients_id' not in context.user_data:
        logger.warning(f"Пользователь {update.effective_user.id} пытается бронировать без регистрации")
        await update.effective_message.reply_text(
            "Сначала нужно зарегистрироваться — это одна минута:",
            reply_markup=InlineKeyboardMarkup([[start_button()]]),
        )
        return ConversationHandler.END

    logger.info(f"Пользователь {update.effective_user.id} начал бронирование")
    await show_bath_keyboard(update, context)
    return CHOOSE_BATH


async def choose_bath(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает выбор бани и показывает выбор типа дня"""
    query = update.callback_query
    await query.answer()

    if query.data == "book_cancel":
        logger.info(f"Пользователь {update.effective_user.id} отменил бронирование")
        await query.edit_message_text("Бронирование отменено.")
        return ConversationHandler.END

    bath_id = query.data.split("_")[1]
    context.user_data['bath_id'] = bath_id
    bath = BATHS[bath_id]
    logger.info(f"Пользователь {update.effective_user.id} выбрал баню: {bath['name']}")

    keyboard = [
        [InlineKeyboardButton("Будни (Пн-Пт)", callback_data="day_weekday")],
        [InlineKeyboardButton("Выходные (Сб-Вс)", callback_data="day_weekend")],
        [InlineKeyboardButton("⬅️ Назад к баням", callback_data="back_bath")],
        [InlineKeyboardButton("❌ Отмена", callback_data="book_cancel")]
    ]
    await query.edit_message_text(
        f"Баня: {bath['name']}\n\nКогда планируете прийти?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CHOOSE_DAY_TYPE


async def choose_day_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает выбор будни/выходные и показывает выбор процедур"""
    query = update.callback_query
    await query.answer()

    if query.data == "book_cancel":
        logger.info(f"Пользователь {update.effective_user.id} отменил бронирование")
        await query.edit_message_text("Бронирование отменено.")
        return ConversationHandler.END

    if query.data == "back_bath":
        logger.info(f"Пользователь {update.effective_user.id} вернулся к выбору бани")
        await show_bath_keyboard(update, context)
        return CHOOSE_BATH

    day_type = "weekday" if query.data == "day_weekday" else "weekend"
    context.user_data['day_type'] = day_type
    bath = BATHS[context.user_data['bath_id']]
    day_text = "Будни" if day_type == "weekday" else "Выходные"
    logger.info(f"Пользователь {update.effective_user.id} выбрал дни: {day_text}")

    keyboard = [
        [InlineKeyboardButton("С парением/процедурами", callback_data="proc_yes")],
        [InlineKeyboardButton("Без процедур", callback_data="proc_no")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_day_type")],
        [InlineKeyboardButton("❌ Отмена", callback_data="book_cancel")]
    ]
    await query.edit_message_text(
        f"Баня: {bath['name']}\nДни: {day_text}\n\nНужны процедуры/парение?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WITH_PROCEDURES


async def procedures_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает выбор с процедурами/без и запрашивает количество гостей"""
    query = update.callback_query
    await query.answer()

    if query.data == "book_cancel":
        logger.info(f"Пользователь {update.effective_user.id} отменил бронирование")
        await query.edit_message_text("Бронирование отменено.")
        return ConversationHandler.END

    if query.data == "back_day_type":
        bath = BATHS[context.user_data['bath_id']]
        logger.info(f"Пользователь {update.effective_user.id} вернулся к выбору типа дня")
        keyboard = [
            [InlineKeyboardButton("Будни (Пн-Пт)", callback_data="day_weekday")],
            [InlineKeyboardButton("Выходные (Сб-Вс)", callback_data="day_weekend")],
            [InlineKeyboardButton("⬅️ Назад к баням", callback_data="back_bath")],
            [InlineKeyboardButton("❌ Отмена", callback_data="book_cancel")]
        ]
        await query.edit_message_text(
            f"Баня: {bath['name']}\n\nКогда планируете прийти?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return CHOOSE_DAY_TYPE

    context.user_data['with_procedures'] = query.data == "proc_yes"
    bath = BATHS[context.user_data['bath_id']]
    day_text = "Будни" if context.user_data['day_type'] == "weekday" else "Выходные"
    proc_text = "с процедурами" if context.user_data['with_procedures'] else "без процедур"
    logger.info(f"Пользователь {update.effective_user.id} выбрал процедуры: {proc_text}")

    await query.edit_message_text(
        f"Баня: {bath['name']}\nДни: {day_text}\nПроцедуры: {proc_text}\n\n"
        f"Сколько человек придёт? Напишите число.\n"
        f"Стандартно до {bath['max_guests']} гостей включительно.\n"
        f"Если больше — мы свяжем вас с менеджером для индивидуальной брони."
    )
    return GUEST_COUNT


async def guest_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает ввод количества гостей"""
    try:
        count = int(update.message.text)
        bath = BATHS[context.user_data['bath_id']]

        if count > bath['max_guests']:
            logger.info(
                f"Пользователь {update.effective_user.id} запросил {count} гостей (больше лимита {bath['max_guests']})")
            keyboard = [
                [InlineKeyboardButton("📞 Связаться с менеджером", callback_data="contact_manager")],
                [InlineKeyboardButton("⬅️ Изменить количество", callback_data="back_guest_count")],
                [InlineKeyboardButton("❌ Отмена", callback_data="book_cancel")]
            ]
            await update.message.reply_text(
                f"Для компании из {count} человек нужно индивидуальное согласование.\n\n"
                f"Баня '{bath['name']}' рассчитана до {bath['max_guests']} гостей.\n\n"
                f"Нажмите кнопку ниже, и менеджер поможет организовать ваш отдых:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return GUEST_COUNT

        if count < 1:
            logger.warning(f"Пользователь {update.effective_user.id} ввёл некорректное количество гостей: {count}")
            await update.message.reply_text("Минимум 1 человек. Сколько придёт?")
            return GUEST_COUNT

    except ValueError:
        logger.warning(f"Пользователь {update.effective_user.id} ввёл не число в количестве гостей")
        await update.message.reply_text("Нужно ввести число. Сколько человек придёт?")
        return GUEST_COUNT

    context.user_data['guest_count'] = count
    context.user_data['procedures'] = []
    logger.info(f"Пользователь {update.effective_user.id} указал количество гостей: {count}")

    if context.user_data['with_procedures']:
        procedures = await get_procedures()
        if procedures:
            await send_menu_photos(update, context)
            text, markup = procedures_screen(procedures, [], count, booking_header(context))
            await update.message.reply_text(text, reply_markup=markup)
            return PICK_PROCEDURES
        # Прайс не отдался — не держим человека на пустом экране, пусть
        # бронирует баню, а процедуры согласует менеджер.
        logger.error("Не удалось получить процедуры, пропускаем шаг выбора")

    await show_date_keyboard_message(update, context, booking_header(context))
    return BOOK_DATE


async def contact_manager_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает запрос связи с менеджером при превышении лимита гостей"""
    query = update.callback_query
    await query.answer()

    if query.data == "book_cancel":
        logger.info(f"Пользователь {update.effective_user.id} отменил бронирование")
        await query.edit_message_text("Бронирование отменено.")
        context.user_data.pop('bath_id', None)
        context.user_data.pop('day_type', None)
        context.user_data.pop('with_procedures', None)
        return ConversationHandler.END

    if query.data == "back_guest_count":
        bath = BATHS[context.user_data['bath_id']]
        day_text = "Будни" if context.user_data['day_type'] == "weekday" else "Выходные"
        proc_text = "с процедурами" if context.user_data['with_procedures'] else "без процедур"
        logger.info(f"Пользователь {update.effective_user.id} вернулся к вводу количества гостей")
        await query.edit_message_text(
            f"Баня: {bath['name']}\nДни: {day_text}\nПроцедуры: {proc_text}\n\n"
            f"Сколько человек придёт? Напишите число.\n"
            f"Стандартно до {bath['max_guests']} гостей включительно."
        )
        return GUEST_COUNT

    if query.data == "contact_manager":
        bath = BATHS[context.user_data['bath_id']]
        day_text = "Будни" if context.user_data['day_type'] == "weekday" else "Выходные"
        proc_text = "с процедурами" if context.user_data['with_procedures'] else "без процедур"
        name = context.user_data['name']
        phone = context.user_data['phone']

        logger.info(f"Пользователь {update.effective_user.id} запросил связь с менеджером")
        await query.edit_message_text(
            f"📞 Контакты менеджера:\n\n"
            f"👤 {MANAGER_NAME}\n"
            f"📱 {MANAGER_PHONE}\n\n"
            f"Ваша заявка:\n"
            f"Баня: {bath['name']}\n"
            f"Дни: {day_text}\n"
            f"Процедуры: {proc_text}\n"
            f"Гостей: больше {bath['max_guests']}\n\n"
            f"Позвоните или напишите менеджеру.\n"
            f"Он уже видит ваши данные: {name}, {phone}",
            reply_markup=InlineKeyboardMarkup([
                [manager_button()],
                [book_button("🌿 Забронировать заново")],
            ])
        )
        # Раньше здесь был user_data.clear() — он стирал и регистрацию,
        # после чего бот требовал регистрироваться заново. Чистим только бронь.
        clear_booking_data(context)
        return ConversationHandler.END


def _photo_files(*dirs: Path) -> list:
    """Собирает фотографии из папок: сначала первая папка целиком, потом вторая,
    внутри каждой — по имени файла. Отсутствующие папки просто пропускаются."""
    files = []
    for directory in dirs:
        if directory.is_dir():
            files += sorted(
                path for path in directory.glob("*")
                if path.suffix.lower() in (".jpg", ".jpeg", ".png")
            )
    return files


async def send_photo_album(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           dirs: list, cache_key: str, caption: str) -> None:
    """
    Отправляет альбом фотографий из указанных папок.

    Первый раз файлы уходят с диска, дальше — по file_id, который Telegram
    выдал на загрузку: так альбом не перезаливается каждому гостю заново.
    file_id живут в bot_data и переживают перезапуск вместе с персистентностью.

    Ошибки только логируются: не отправились фото — разговор всё равно продолжится.

    Args:
        dirs: папки с фото, в том порядке, в каком они должны идти в альбоме
        cache_key: под каким ключом в bot_data держать file_id этого альбома
        caption: подпись — Telegram показывает её у первой фотографии
    """
    cached = context.bot_data.get(cache_key)

    try:
        if cached:
            media = [InputMediaPhoto(file_id) for file_id in cached]
        else:
            files = _photo_files(*dirs)
            if not files:
                logger.info(f"Фото не найдены в {', '.join(str(d) for d in dirs)}, пропускаем")
                return
            # Telegram не берёт в один альбом больше десяти фотографий
            media = [InputMediaPhoto(path.read_bytes()) for path in files[:10]]

        media[0] = InputMediaPhoto(media[0].media, caption=caption)
        messages = await update.effective_message.reply_media_group(media=media)

        if not cached:
            context.bot_data[cache_key] = [m.photo[-1].file_id for m in messages]
            logger.info(f"Альбом «{caption}» загружен и закэширован: {len(messages)} шт.")
    except Exception as e:
        # Битый file_id после смены бота — сбрасываем кэш, в следующий раз зальём заново
        context.bot_data.pop(cache_key, None)
        logger.error(f"Не удалось отправить альбом «{caption}»: {e}")


async def send_menu_photos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Альбом с фотографиями банного меню — уходит перед выбором процедур."""
    await send_photo_album(update, context, [MENU_PHOTOS_DIR], "menu_file_ids", "🧖 Банное меню")


def procedures_screen(procedures: list, chosen: list, needed: int, header: str) -> tuple:
    """
    Готовит текст и клавиатуру выбора процедур.

    Процедур нужно ровно столько же, сколько гостей: по одной на человека.
    Повторы разрешены — трое могут взять одно и то же.

    Returns:
        tuple: (текст сообщения, InlineKeyboardMarkup)
    """
    index = {p["id"]: p for p in procedures}
    picked_lines = [
        f"  • {index[service_id]['title']}" + (f" × {count}" if count > 1 else "")
        for service_id, count in Counter(chosen).items() if service_id in index
    ]

    text = (
        f"{header}\n\n"
        f"Выберите процедуры — по одной на каждого гостя.\n"
        f"Одну и ту же можно взять несколько раз.\n\n"
        f"Выбрано: {len(chosen)} из {needed}"
    )
    if picked_lines:
        text += "\n" + "\n".join(picked_lines)

    keyboard = []
    if len(chosen) < needed:
        # Только название: цены и длительность гость уже видел на фото меню
        for procedure in procedures:
            keyboard.append([InlineKeyboardButton(
                procedure['title'], callback_data=f"pick_{procedure['id']}"
            )])
    if chosen:
        keyboard.append([InlineKeyboardButton("↩️ Убрать последнюю", callback_data="pick_undo")])
    if len(chosen) == needed:
        keyboard.append([InlineKeyboardButton("✅ Дальше, к выбору даты", callback_data="pick_done")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_procedures")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="book_cancel")])

    return text, InlineKeyboardMarkup(keyboard)


def booking_header(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Шапка «баня / дни / процедуры / гостей» — она повторяется на всех экранах."""
    bath = BATHS[context.user_data['bath_id']]
    day_text = "Будни" if context.user_data['day_type'] == "weekday" else "Выходные"
    proc_text = "с процедурами" if context.user_data['with_procedures'] else "без процедур"
    return (f"Баня: {bath['name']}\nДни: {day_text}\nПроцедуры: {proc_text}\n"
            f"Гостей: {context.user_data['guest_count']}")


def build_date_buttons(day_type: str, days_ahead: int = DAYS_AHEAD) -> list:
    """
    Строит кнопки выбора даты, показывая ТОЛЬКО дни, подходящие под выбранный тип.

    Отсчёт идёт с завтрашнего дня: баню нужно успеть протопить, записи «на сегодня»
    через бота не принимаются — за ними отправляем к менеджеру.

    Args:
        day_type: "weekday" (Пн-Пт) или "weekend" (Сб-Вс)
        days_ahead: на сколько дней вперёд показывать

    Returns:
        list: список рядов кнопок по DATE_COLUMNS штук (без "Назад"/"Отмена")
    """
    buttons = []
    row = []
    today = _today_local()
    days_ru = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

    for i in range(1, days_ahead + 1):
        day = today + timedelta(days=i)
        # Пропускаем даты, не соответствующие выбору пользователя
        if day_type == "weekend" and not is_weekend(day):
            continue
        if day_type == "weekday" and is_weekend(day):
            continue
        day_name = days_ru[day.weekday()]
        date_str = day.strftime(f"%d.%m {day_name}")
        date_iso = day.strftime("%Y-%m-%d")
        row.append(InlineKeyboardButton(date_str, callback_data=f"date_{date_iso}"))
        if len(row) == DATE_COLUMNS:
            buttons.append(row)
            row = []

    if row:
        buttons.append(row)

    logger.info(f"Показано дат для day_type={day_type}: {sum(len(r) for r in buttons)}")
    return buttons


def date_markup(day_type: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора даты вместе с навигацией — одна и та же на всех экранах,
    куда можно вернуться к выбору даты."""
    keyboard = build_date_buttons(day_type)
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_procedures")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="book_cancel")])
    return InlineKeyboardMarkup(keyboard)


def build_time_buttons(bath_id: str, day_type: str, with_proc: bool, free_starts: list) -> list:
    """
    Собирает кнопки под свободные заходы.

    Args:
        free_starts: времена начала из get_free_seances(), например ["10:00"]

    Returns:
        list: ряды кнопок вида "10:00 – 13:00" (без "Назад"/"Отмена")
    """
    buttons = []
    for start in free_starts:
        end = seance_end(start, bath_id, day_type, with_proc)
        buttons.append([InlineKeyboardButton(f"{start} – {end}", callback_data=f"time_{start}")])

    return buttons


async def show_date_keyboard_message(update: Update, context: ContextTypes.DEFAULT_TYPE, bath_name: str):
    """Показывает клавиатуру выбора даты на DAYS_AHEAD дней вперёд"""
    await update.message.reply_text(
        f"{bath_name}\nВыберите дату:",
        reply_markup=date_markup(context.user_data['day_type']),
    )


async def pick_procedures(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Набор процедур: по одной на гостя, повторы разрешены"""
    query = update.callback_query
    await query.answer()

    if query.data == "book_cancel":
        logger.info(f"Пользователь {update.effective_user.id} отменил бронирование")
        await query.edit_message_text("Бронирование отменено.")
        return ConversationHandler.END

    if query.data == "back_procedures":
        bath = BATHS[context.user_data['bath_id']]
        day_text = "Будни" if context.user_data['day_type'] == "weekday" else "Выходные"
        context.user_data['procedures'] = []
        logger.info(f"Пользователь {update.effective_user.id} вернулся к выбору процедур")
        keyboard = [
            [InlineKeyboardButton("С парением/процедурами", callback_data="proc_yes")],
            [InlineKeyboardButton("Без процедур", callback_data="proc_no")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_day_type")],
            [InlineKeyboardButton("❌ Отмена", callback_data="book_cancel")]
        ]
        await query.edit_message_text(
            f"Баня: {bath['name']}\nДни: {day_text}\n\nНужны процедуры/парение?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return WITH_PROCEDURES

    chosen = context.user_data.setdefault('procedures', [])
    needed = context.user_data['guest_count']

    if query.data == "pick_done":
        if len(chosen) != needed:
            await query.answer(f"Нужно выбрать ровно {needed}", show_alert=True)
            return PICK_PROCEDURES
        logger.info(f"Пользователь {update.effective_user.id} выбрал процедуры: {chosen}")
        await query.edit_message_text(
            f"{booking_header(context)}\nВыберите дату:",
            reply_markup=date_markup(context.user_data['day_type'])
        )
        return BOOK_DATE

    if query.data == "pick_undo":
        if chosen:
            chosen.pop()
    else:
        if len(chosen) >= needed:
            await query.answer(f"Уже выбрано {needed}, больше не нужно", show_alert=True)
            return PICK_PROCEDURES
        chosen.append(int(query.data.split("_")[1]))

    procedures = await get_procedures()
    text, markup = procedures_screen(procedures, chosen, needed, booking_header(context))
    await query.edit_message_text(text, reply_markup=markup)
    return PICK_PROCEDURES


async def book_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает выбор даты и показывает доступное время"""
    query = update.callback_query
    await query.answer()

    if query.data == "book_cancel":
        logger.info(f"Пользователь {update.effective_user.id} отменил бронирование")
        await query.edit_message_text("Бронирование отменено.")
        return ConversationHandler.END

    if query.data == "back_procedures":
        bath = BATHS[context.user_data['bath_id']]
        day_text = "Будни" if context.user_data['day_type'] == "weekday" else "Выходные"
        logger.info(f"Пользователь {update.effective_user.id} вернулся к выбору процедур")
        keyboard = [
            [InlineKeyboardButton("С парением/процедурами", callback_data="proc_yes")],
            [InlineKeyboardButton("Без процедур", callback_data="proc_no")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_day_type")],
            [InlineKeyboardButton("❌ Отмена", callback_data="book_cancel")]
        ]
        await query.edit_message_text(
            f"Баня: {bath['name']}\nДни: {day_text}\n\nНужны процедуры/парение?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return WITH_PROCEDURES

    if query.data == "back_date":
        bath = BATHS[context.user_data['bath_id']]
        day_text = "Будни" if context.user_data['day_type'] == "weekday" else "Выходные"
        proc_text = "с процедурами" if context.user_data['with_procedures'] else "без процедур"
        guest_count = context.user_data['guest_count']
        logger.info(f"Пользователь {update.effective_user.id} вернулся к выбору даты")

        await query.edit_message_text(
            f"{bath['name']}\n{day_text}\n{proc_text}\nГостей: {guest_count}\nВыберите дату:",
            reply_markup=date_markup(context.user_data['day_type'])
        )
        return BOOK_DATE

    date_iso = query.data.split("_")[1]
    date_obj = datetime.strptime(date_iso, "%Y-%m-%d")

    # Кнопки строятся с завтрашнего дня, но сообщение могло провисеть до полуночи —
    # тогда вчерашняя кнопка «на завтра» указывает на сегодня. Ловим это здесь.
    if date_obj.date() <= _today_local():
        logger.info(f"Пользователь {update.effective_user.id} выбрал сегодняшнюю или прошедшую дату {date_iso}")
        await query.answer("На сегодня записаться уже нельзя — выберите другой день", show_alert=True)
        return BOOK_DATE

    # Проверяем соответствие выбранных будни/выходные и реальной даты
    is_weekend_selected = is_weekend(date_obj)
    day_type_selected = context.user_data['day_type']

    if (day_type_selected == "weekday" and is_weekend_selected) or (
            day_type_selected == "weekend" and not is_weekend_selected):
        logger.warning(f"Пользователь {update.effective_user.id} выбрал дату не соответствующую типу дня")
        await query.answer("Эта дата не соответствует выбранному типу дней", show_alert=True)
        return BOOK_DATE

    context.user_data['book_date'] = date_iso
    bath = BATHS[context.user_data['bath_id']]
    day_text = "Будни" if context.user_data['day_type'] == "weekday" else "Выходные"
    proc_text = "с процедурами" if context.user_data['with_procedures'] else "без процедур"
    guest_count = context.user_data['guest_count']
    service_id = get_service_id(context.user_data['bath_id'], context.user_data['day_type'],
                                context.user_data['with_procedures'])
    logger.info(f"Пользователь {update.effective_user.id} выбрал дату: {date_iso}, service_id={service_id}")

    await query.edit_message_text(
        f"Баня: {bath['name']}\nДни: {day_text}\nПроцедуры: {proc_text}\nГостей: {guest_count}\nДата: {date_iso}\nИщу свободное время... ⏳"
    )

    free_starts = await get_free_seances(
        context.user_data['bath_id'], context.user_data['day_type'],
        context.user_data['with_procedures'], date_iso
    )
    keyboard = build_time_buttons(
        context.user_data['bath_id'], context.user_data['day_type'],
        context.user_data['with_procedures'], free_starts
    )

    if not keyboard:
        logger.info(f"Нет свободных сеансов для даты {date_iso}, service_id={service_id}")
        await query.edit_message_text(
            f"Баня: {bath['name']}\nДата: {date_iso}\n\n"
            f"На эту дату свободных сеансов нет 😔\n\n"
            f"Выберите другой день или свяжитесь с менеджером — "
            f"он подскажет, что можно придумать.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Выбрать другую дату", callback_data="back_date")],
                [manager_button()],
                [InlineKeyboardButton("❌ Отмена", callback_data="book_cancel")],
            ])
        )
        return BOOK_DATE

    keyboard.append([manager_button("📞 Не подходит время — написать менеджеру")])
    keyboard.append([InlineKeyboardButton("⬅️ Другая дата", callback_data="back_date")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="book_cancel")])

    await query.edit_message_text(
        f"Баня: {bath['name']}\nДни: {day_text}\nПроцедуры: {proc_text}\nГостей: {guest_count}\nДата: {date_iso}\n\n"
        f"Выберите сеанс:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return BOOK_TIME


async def book_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает выбор времени и создаёт запись в YCLIENTS"""
    query = update.callback_query
    await query.answer()

    if query.data == "book_cancel":
        logger.info(f"Пользователь {update.effective_user.id} отменил бронирование")
        await query.edit_message_text("Бронирование отменено.")
        return ConversationHandler.END

    if query.data == "back_date":
        bath = BATHS[context.user_data['bath_id']]
        day_text = "Будни" if context.user_data['day_type'] == "weekday" else "Выходные"
        proc_text = "с процедурами" if context.user_data['with_procedures'] else "без процедур"
        guest_count = context.user_data['guest_count']
        logger.info(f"Пользователь {update.effective_user.id} вернулся к выбору даты")

        await query.edit_message_text(
            f"{bath['name']}\n{day_text}\n{proc_text}\nГостей: {guest_count}\nВыберите дату:",
            reply_markup=date_markup(context.user_data['day_type'])
        )
        return BOOK_DATE

    time_str = query.data.split("_")[1]
    date_iso = context.user_data['book_date']
    bath = BATHS[context.user_data['bath_id']]
    day_text = "Будни" if context.user_data['day_type'] == "weekday" else "Выходные"
    proc_text = "с процедурами" if context.user_data['with_procedures'] else "без процедур"
    guest_count = context.user_data['guest_count']
    datetime_str = f"{date_iso} {time_str}:00"
    service_id = get_service_id(context.user_data['bath_id'], context.user_data['day_type'],
                                context.user_data['with_procedures'])
    seance_length = get_seance_length(context.user_data['bath_id'], context.user_data['day_type'],
                                      context.user_data['with_procedures'])
    logger.info(f"Пользователь {update.effective_user.id} выбрал время: {time_str}, создаём запись")

    await query.edit_message_text(f"Бронирую {bath['name']} на {date_iso} в {time_str}... ⏳")

    # Аренда бани плюс процедуры — одной записью, чтобы YCLIENTS сам посчитал
    # стоимость и всё легло в отчёты. Повторы схлопываем в amount.
    chosen = context.user_data.get('procedures') or []
    services = [{"id": service_id, "amount": 1}]
    services += [{"id": pid, "amount": count} for pid, count in Counter(chosen).items()]

    index = await get_services_index()
    bath_title = (index.get(service_id) or {}).get("title") or f"Аренда бани «{bath['name']}»"
    lines, total = price_breakdown(bath_title, (index.get(service_id) or {}).get("price") or 0, chosen, index)

    procedures_text = ", ".join(
        f"{(index.get(pid) or {}).get('title', pid)}" + (f" ×{count}" if count > 1 else "")
        for pid, count in Counter(chosen).items()
    ) or "нет"

    comment = (f"Бронь {bath['name']} через Telegram бота\nДни: {day_text}\nПроцедуры: {proc_text}\n"
               f"Гостей: {guest_count}\nБанное меню: {procedures_text}")

    success = await create_booking(
        yclients_id=int(context.user_data['yclients_id']),
        staff_id=int(bath['staff_id']),
        services=services,
        datetime_str=datetime_str,
        comment=comment,
        seance_length=seance_length
    )

    end_str = seance_end(time_str, context.user_data['bath_id'], context.user_data['day_type'],
                         context.user_data['with_procedures'])

    if success:
        logger.info(f"Бронирование успешно создано для пользователя {update.effective_user.id}")
        details = {
            "name": context.user_data.get('name', '—'),
            "phone": context.user_data.get('phone', '—'),
            "bath": bath['name'],
            "date": date_iso,
            "start": time_str,
            "end": end_str,
            "proc_text": proc_text,
            "guests": guest_count,
            "procedures": procedures_text,
            "total": total,
        }
        # Уведомление админам уходит фоном: гость не должен ждать лишний
        # round-trip к Telegram, чтобы увидеть «Готово». create_task от
        # приложения, а не голый asyncio, — тогда PTB дождётся его при остановке.
        context.application.create_task(
            notify_group_about_booking(context, update.effective_user, details)
        )
        # Ставим бронь в очередь автоматических писем: напоминание накануне
        # и просьба об отзыве наутро после визита.
        remember_booking(context, update.effective_chat.id, details)
        await query.edit_message_text(
            f"Готово! Баня забронирована ✅\n\n"
            f"🌿 Баня: {bath['name']}\n"
            f"📅 Дата: {date_iso} ({day_text})\n"
            f"⏰ Сеанс: {time_str} – {end_str}\n"
            f"👥 Гостей: {guest_count}\n\n"
            + "\n".join(lines) + "\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💰 Итого: {money(total)}\n\n"
            f"По поводу предоплаты вам в скором времени напишет менеджер.\n\n"
            f"Нужно больше времени? Напишите или позвоните:\n"
            f"👤 {MANAGER_NAME}\n"
            f"📱 {MANAGER_PHONE}\n\n"
            f"Ждём вас!",
            reply_markup=InlineKeyboardMarkup([[manager_button()]])
        )
    else:
        logger.error(f"Не удалось создать бронирование для пользователя {update.effective_user.id}")
        await query.edit_message_text(
            "Не удалось забронировать 😔\nСеанс уже заняли, либо случилась ошибка.",
            reply_markup=InlineKeyboardMarkup([
                [book_button("🔄 Попробовать ещё раз")],
                [manager_button()],
            ])
        )

    # Очищаем данные бронирования из context
    context.user_data.pop('book_date', None)
    context.user_data.pop('bath_id', None)
    context.user_data.pop('day_type', None)
    context.user_data.pop('with_procedures', None)
    context.user_data.pop('guest_count', None)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logger.info(f"Пользователь {update.effective_user.id} отменил действие")
    end_all_conversations(update, context.application)
    clear_booking_data(context)
    message = update.effective_message
    if message:
        await message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
        await message.reply_text(
            "Что дальше?",
            reply_markup=InlineKeyboardMarkup([
                [book_button()],
                [start_button("📝 Регистрация заново")],
            ]),
        )
    return ConversationHandler.END


async def force_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальный /start: сбрасывает зависшее бронирование и запускает регистрацию."""
    end_all_conversations(update, context.application)
    clear_booking_data(context)
    reg_handler = HANDLERS.get("reg_handler")
    if reg_handler:
        key = reg_handler._get_key(update)
        reg_handler._conversations[key] = NAME
    await start(update, context)
    # Останавливаем обработку: иначе reg_handler в группе 1 поймает /start
    # своим entry_point и отправит приветствие второй раз.
    raise ApplicationHandlerStop


async def force_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальный /cancel: всегда отвечает, даже если диалог завис."""
    end_all_conversations(update, context.application)
    clear_booking_data(context)
    message = update.effective_message
    if message:
        await message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
        await message.reply_text(
            "Что дальше?",
            reply_markup=InlineKeyboardMarkup([
                [book_button()],
                [start_button("📝 Регистрация заново")],
            ]),
        )
    # Иначе fallback cancel активного диалога ответит вторым сообщением
    raise ApplicationHandlerStop


async def force_book(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальный /book: перезапускает бронирование, даже если старый диалог завис."""
    end_all_conversations(update, context.application)
    clear_booking_data(context)
    book_handler = HANDLERS.get("book_handler")
    if book_handler:
        key = book_handler._get_key(update)
        state = await book_start(update, context)
        book_handler._conversations[key] = state
    else:
        await book_start(update, context)
    # Останавливаем обработку: иначе book_handler в группе 1 поймает /book
    # своим entry_point и отправит "Выберите баню" второй раз.
    raise ApplicationHandlerStop


async def go_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «Зарегистрироваться» — делает то же, что команда /start."""
    await update.callback_query.answer()
    await force_start(update, context)  # внутри поднимает ApplicationHandlerStop


async def go_book_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «Забронировать баню» — делает то же, что команда /book."""
    await update.callback_query.answer()
    await force_book(update, context)  # внутри поднимает ApplicationHandlerStop


async def chat_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Служебная команда: показывает ID текущего чата.

    Нужна ровно один раз — чтобы узнать ID рабочей группы и прописать его
    в BOOKING_GROUP_CHAT_ID. Отправьте /chatid в самой группе.
    """
    chat = update.effective_chat
    logger.info(f"Запрошен ID чата: {chat.id} ({chat.type})")
    await update.effective_message.reply_text(
        f"ID этого чата: {chat.id}\n"
        f"Тип: {chat.type}\n\n"
        f"Впишите это число в переменную BOOKING_GROUP_CHAT_ID и перезапустите бота — "
        f"сюда начнут приходить уведомления о новых бронях."
    )
    # Иначе команду подхватит fallback активного диалога и ответит «не понял»
    raise ApplicationHandlerStop


async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/price — прайс-лист альбомом: сначала аренда бань, следом банное меню.

    Работает в любой момент, в том числе посреди брони: показать цены и вернуться
    к выбору — обычное дело, сбивать из-за этого диалог незачем.
    """
    logger.info(f"Пользователь {update.effective_user.id} запросил прайс-лист")
    await send_photo_album(
        update, context, [PRICE_PHOTOS_DIR, MENU_PHOTOS_DIR], "price_file_ids", "💰 Прайс-лист"
    )
    await update.effective_message.reply_text(
        "Остались вопросы или готовы бронировать?",
        reply_markup=InlineKeyboardMarkup([[book_button()], [manager_button()]]),
    )
    # Иначе команду подхватит fallback активного диалога и ответит «не понял»
    raise ApplicationHandlerStop


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /help"""
    await update.message.reply_text(
        "Чем помочь?\n\n"
        "Кнопки ниже делают то же, что команды /start, /book и /cancel — "
        "набирать их руками не обязательно.",
        reply_markup=InlineKeyboardMarkup([
            [book_button()],
            [start_button("📝 Регистрация заново")],
            [manager_button()],
        ]),
    )


async def invalid_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Фолбэк для ввода, не подошедшего под текущий шаг диалога.
    Ничего не возвращает — ConversationHandler оставляет состояние без изменений."""
    if update.callback_query:
        await update.callback_query.answer("Эта кнопка уже неактуальна 🤔", show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(
            "Не понял 🤔 Пожалуйста, используйте кнопки или следуйте подсказке выше."
        )


# Когда какая ошибка последний раз улетала админу: {текст ошибки: время}.
# Обычная переменная модуля, а не bot_data: пиклить это незачем, после
# перезапуска логично сообщить о проблеме заново.
ERRORS_SENT: dict = {}


def _should_notify(error_key: str) -> bool:
    """Не даёт слать одну и ту же ошибку чаще, чем раз в ERROR_NOTIFY_INTERVAL_MINUTES."""
    now = datetime.now()
    last = ERRORS_SENT.get(error_key)
    if last and (now - last) < timedelta(minutes=ERROR_NOTIFY_INTERVAL_MINUTES):
        return False
    # Словарь не должен расти бесконечно на потоке разных ошибок
    if len(ERRORS_SENT) > 100:
        ERRORS_SENT.clear()
    ERRORS_SENT[error_key] = now
    return True


def _error_context(update: object) -> str:
    """Описывает, на чём споткнулся бот: кто и что нажал или написал."""
    if not isinstance(update, Update):
        return "Вне обработки сообщения"

    lines = []
    user = update.effective_user
    if user:
        who = f"{user.full_name} (id {user.id}"
        who += f", @{user.username})" if user.username else ")"
        lines.append(f"Гость: {who}")
    if update.callback_query:
        lines.append(f"Нажал кнопку: {update.callback_query.data}")
    elif update.effective_message and update.effective_message.text:
        lines.append(f"Написал: {update.effective_message.text[:200]}")
    return "\n".join(lines) or "Обновление без пользователя"


async def notify_admin_about_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Присылает администратору в личку карточку ошибки с концом трассировки.

    Сама ничего не поднимает наверх: если уж и уведомление не отправилось,
    то падать в обработчике ошибок — последнее, что боту стоит делать.
    """
    if not ADMIN_CHAT_ID:
        return

    error = context.error
    error_key = f"{type(error).__name__}: {error}"
    if not _should_notify(error_key):
        logger.info("Такая ошибка уже отправлена недавно, админу не дублируем")
        return

    tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    text = (
        f"\u26A0\uFE0F Ошибка в боте\n\n"
        f"{error_key}\n\n"
        f"{_error_context(update)}\n\n"
        f"Где именно (конец трассировки):\n{tb[-1200:]}"
    )

    try:
        # Без parse_mode: в трассировке хватает символов, на которых разметка ломается
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text[:4096])
        logger.info(f"Ошибка отправлена администратору в чат {ADMIN_CHAT_ID}")
    except Exception as e:
        logger.error(f"Не удалось отправить ошибку администратору: {e}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальный обработчик ошибок — не даёт боту падать на некритичных ошибках Telegram API"""
    if isinstance(context.error, BadRequest) and "Message is not modified" in str(context.error):
        # Пользователь нажал ту же кнопку дважды подряд — просто игнорируем
        return
    logger.error(f"Необработанная ошибка: {context.error}", exc_info=context.error)
    await notify_admin_about_error(update, context)


# Меню команд — то, что Telegram показывает по кнопке «Menu» рядом с полем ввода.
# Служебную /chatid сюда не кладём: она нужна администратору один раз при настройке.
BOT_COMMANDS = [
    BotCommand("start", "Меню бота и регистрация"),
    BotCommand("book", "Забронировать баню"),
    BotCommand("price", "Прайс-лист"),
    BotCommand("help", "Помощь и контакты"),
    BotCommand("cancel", "Отменить текущее действие"),
]


async def post_init(application: Application) -> None:
    """Выполняется один раз при старте — прописывает меню команд в Telegram."""
    await application.bot.set_my_commands(BOT_COMMANDS)
    logger.info("Меню команд бота обновлено")


async def post_shutdown(application: Application) -> None:
    """Выполняется при остановке — закрывает общий HTTP-клиент."""
    await close_http_client()


def main() -> None:
    """запуск бота"""
    check_config()
    persistence = PicklePersistence(filepath=PERSISTENCE_FILE)
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        # Иначе гости обслуживаются строго по одному: пока один ждёт ответа
        # YCLIENTS, у всех остальных кнопки висят. Вечером пятницы это заметно.
        .concurrent_updates(True)
        .build()
    )
    #хендлер регистрации
    reg_handler = ConversationHandler(
        name="registration",
        persistent=True,
        entry_points=[CommandHandler("start", start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone)],
            CONFIRM: [MessageHandler(filters.Regex(r"^(✅ Всё верно|❌ Изменить)$"), confirm)]
        },
        # CommandHandler'ы на /start и /cancel сюда не добавляем: команды всегда
        # перехватываются в group=0 (force_start/force_cancel) и дальше не идут,
        # так что такие fallback'и были бы мёртвым кодом.
        fallbacks=[MessageHandler(filters.ALL, invalid_input)],
    )
    # Хэндлер бронирования
    book_handler = ConversationHandler(
        name="booking",
        persistent=True,
        allow_reentry=True,
        entry_points=[CommandHandler("book", book_start)],
        states={
            CHOOSE_BATH: [CallbackQueryHandler(choose_bath, pattern="^bath_|book_cancel")],
            CHOOSE_DAY_TYPE: [CallbackQueryHandler(choose_day_type, pattern="^day_|back_bath|book_cancel")],
            WITH_PROCEDURES: [CallbackQueryHandler(procedures_callback, pattern="^proc_|back_day_type|book_cancel")],
            GUEST_COUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, guest_count),
                CallbackQueryHandler(contact_manager_callback, pattern="^contact_manager|back_guest_count|book_cancel")
            ],
            PICK_PROCEDURES: [
                CallbackQueryHandler(pick_procedures, pattern="^pick_|back_procedures|book_cancel")
            ],
            BOOK_DATE: [CallbackQueryHandler(book_date, pattern="^date_|back_procedures|back_date|book_cancel")],
            BOOK_TIME: [CallbackQueryHandler(book_time, pattern="^time_|back_date|book_cancel")],
        },
        # CommandHandler'ы на /start и /cancel сюда не добавляем по той же причине,
        # что и в reg_handler — group=0 их перехватывает раньше.
        fallbacks=[MessageHandler(filters.ALL, invalid_input), CallbackQueryHandler(invalid_input)],
    )

    # Храним в обычной переменной модуля, а НЕ в bot_data:
    # bot_data сохраняется через PicklePersistence, а ConversationHandler
    # не сериализуется и ломает сохранение состояния.
    HANDLERS["reg_handler"] = reg_handler
    HANDLERS["book_handler"] = book_handler

    # Приоритетные команды — работают даже при зависшем диалоге
    application.add_handler(CommandHandler("start", force_start), group=0)
    application.add_handler(CommandHandler("cancel", force_cancel), group=0)
    application.add_handler(CommandHandler("book", force_book), group=0)

    # Кнопки-дубликаты команд. Тоже в group=0: нажать их можно из старого
    # сообщения, когда активен любой диалог, и сработать они должны так же
    # безусловно, как сами команды.
    application.add_handler(CallbackQueryHandler(go_start_callback, pattern="^go_start$"), group=0)
    application.add_handler(CallbackQueryHandler(go_book_callback, pattern="^go_book$"), group=0)

    # Прайс-лист — тоже в group=0: цены можно спросить и посреди бронирования,
    # диалог при этом должен остаться там же, где был.
    application.add_handler(CommandHandler("price", price_command), group=0)

    # Служебная команда для настройки уведомлений — работает в любом чате
    application.add_handler(CommandHandler("chatid", chat_id_command), group=0)

    application.add_handler(reg_handler, group=1)
    application.add_handler(book_handler, group=1)
    application.add_handler(CommandHandler("help", help_command), group=1)
    application.add_error_handler(error_handler)

    # Автоматические письма клиентам: напоминание накануне визита и просьба об
    # отзыве наутро после него. По одному ежедневному заданию на каждое — так
    # рассылка переживает перезапуск, в отличие от отложенных job'ов на бронь.
    if application.job_queue:
        application.job_queue.run_daily(
            send_day_before_reminders,
            time=REMINDER_TIME.replace(tzinfo=LOCAL_TZ),
            name="day_before_reminders",
        )
        application.job_queue.run_daily(
            send_feedback_requests,
            time=FEEDBACK_TIME.replace(tzinfo=LOCAL_TZ),
            name="feedback_requests",
        )
        # Если бота перезапустили уже после времени рассылки, сегодняшнее задание
        # само отработает только завтра. Догоняем сразу после старта: повторов
        # не будет, отправленное помечено флагами внутри самой брони.
        now = datetime.now(LOCAL_TZ).time()
        if now >= REMINDER_TIME:
            application.job_queue.run_once(send_day_before_reminders, when=10, name="day_before_catchup")
        if now >= FEEDBACK_TIME:
            application.job_queue.run_once(send_feedback_requests, when=10, name="feedback_catchup")
    else:
        logger.error(
            "JobQueue недоступна — напоминания и просьбы об отзыве отправляться НЕ будут. "
            'Поставьте зависимость: pip install "python-telegram-bot[job-queue]"'
        )

    logger.info("Бот запущен")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
