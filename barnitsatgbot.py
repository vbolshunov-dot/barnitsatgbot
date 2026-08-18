#перед запуском бота, скрыть все API и ID
import logging
import os
import re
import sys
import httpx
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
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

# Файл для сохранения состояния диалогов между перезапусками бота
PERSISTENCE_FILE = os.getenv("PERSISTENCE_FILE", "bot_persistence.pickle")


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
    missing = [name for name, value in required.items() if not value]
    if missing:
        print(
            "ОШИБКА: не заданы переменные окружения: " + ", ".join(missing) +
            "\nСоздайте файл .env рядом с этим скриптом (образец — .env.example)",
            file=sys.stderr,
        )
        sys.exit(1)


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
            "with_proc": 4   # берёзовая выходные с процедурами — 04 ч 00 м
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

# настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# состояния для сonversationHandler регистрации
NAME, PHONE, CONFIRM = range(3)
# состояния для сonversationHandler бронирования
CHOOSE_BATH, CHOOSE_DAY_TYPE, WITH_PROCEDURES, GUEST_COUNT, BOOK_DATE, BOOK_TIME = range(3, 9)

# ключи временных данных бронирования (сбрасываются при /cancel и /start)
BOOKING_KEYS = ("bath_id", "day_type", "with_procedures", "guest_count", "book_date")

# регулярка для валидации российского номера телефона
PHONE_REGEX = re.compile(r'^\+7\d{10}$')

# Ссылки на ConversationHandler'ы для force_* команд.
# Хранятся здесь, а не в bot_data: bot_data пиклится персистентностью,
# а ConversationHandler не сериализуется.
HANDLERS: dict = {}


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


async def register_in_yclients(name: str, phone: str, telegram_id: int) -> tuple[bool, int]:
    """
    Регистрирует нового клиента в YCLIENTS или находит существующего по телефону

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
        async with httpx.AsyncClient() as client:
            logger.info(f"Отправляем запрос регистрации в YCLIENTS: {name}, {phone}")
            response = await client.post(url, json=payload, headers=headers, timeout=10.0)
            data = response.json()

            if response.status_code == 201:
                logger.info(f"Клиент успешно зарегистрирован в YCLIENTS: id={data['data']['id']}")
                return True, data['data']['id']
            elif response.status_code == 422:
                # YCLIENTS возвращает 422 в т.ч. когда клиент с этим телефоном уже есть.
                # Текст ошибки может быть на русском, поэтому не матчим конкретную
                # строку — просто пробуем найти клиента по телефону.
                logger.info(f"Регистрация вернула 422 ({data}), ищем клиента по телефону: {phone}")
                search_url = f"https://api.yclients.com/api/v1/clients/{YCLIENTS_COMPANY_ID}"
                search_resp = await client.get(search_url, headers=headers, params={"phone": phone})
                search_data = search_resp.json()
                if search_data.get('data'):
                    logger.info(f"Найден существующий клиент: id={search_data['data'][0]['id']}")
                    return True, search_data['data'][0]['id']

            logger.error(f"Yclients register error: {response.status_code} {response.text}")
            return False, 0
    except Exception as e:
        logger.error(f"Yclients exception: {e}")
        return False, 0


async def get_free_slots(staff_id: int, service_id: int, date: str) -> list:
    """
    Получает список свободных временных слотов из YCLIENTS

    Args:
        staff_id: ID сотрудника (бани) в YCLIENTS
        service_id: ID услуги в YCLIENTS
        date: Дата в формате YYYY-MM-DD

    Returns:
        list: Список слотов с временем
    """
    url = f"https://api.yclients.com/api/v1/book_times/{YCLIENTS_COMPANY_ID}/{staff_id}/{date}"
    headers = {
        "Authorization": yclients_auth_header(),
        "Accept": "application/vnd.yclients.v2+json"
    }
    params = {"service_ids[]": service_id}

    try:
        async with httpx.AsyncClient() as client:
            logger.info(f"Запрашиваем свободные слоты: staff_id={staff_id}, service_id={service_id}, date={date}")
            response = await client.get(url, headers=headers, params=params, timeout=10.0)
            data = response.json()
            slots = data.get('data', [])
            logger.info(f"Получено слотов: {len(slots)}")
            return slots
    except Exception as e:
        logger.error(f"Ошибка получения слотов: {e}")
        return []


async def create_booking(yclients_id: int, staff_id: int, service_id: int, datetime_str: str, comment: str,
                         seance_length: int) -> bool:
    """
    Создаёт запись на услугу в YCLIENTS

    Args:
        yclients_id: ID клиента в YCLIENTS
        staff_id: ID сотрудника (бани)
        service_id: ID услуги
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
        "services": [{"id": service_id}],
        "client": {"id": yclients_id},
        "datetime": datetime_str,
        "seance_length": seance_length,
        "comment": comment
    }

    try:
        async with httpx.AsyncClient() as client:
            logger.info(
                f"Создаём запись в YCLIENTS: client_id={yclients_id}, service_id={service_id}, datetime={datetime_str}")
            response = await client.post(url, json=payload, headers=headers, timeout=10.0)
            if response.status_code == 201:
                logger.info("Запись успешно создана в YCLIENTS")
                return True
            logger.error(f"Ошибка создания записи: {response.status_code} {response.text}")
            return False
    except Exception as e:
        logger.error(f"Exception создания записи: {e}")
        return False


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
    await update.message.reply_html(
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
            "Готово! Вы зарегистрированы ✅\n\nЗабронировать баню: /book"
        )
    else:
        logger.error(f"Ошибка регистрации пользователя {update.effective_user.id}")
        await update.message.reply_text("Ошибка регистрации 😔 Попробуйте /start позже")

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
        await update.message.reply_text("Сначала нужно зарегистрироваться. Жми /start")
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
    logger.info(f"Пользователь {update.effective_user.id} указал количество гостей: {count}")

    bath = BATHS[context.user_data['bath_id']]
    day_text = "Будни" if context.user_data['day_type'] == "weekday" else "Выходные"
    proc_text = "с процедурами" if context.user_data['with_procedures'] else "без процедур"

    await show_date_keyboard_message(update, context, f"{bath['name']}\n{day_text}\n{proc_text}\nГостей: {count}")
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
            f"Позвоните или напишите в WhatsApp/Telegram менеджеру.\n"
            f"Он уже видит ваши данные: {name}, {phone}\n\n"
            f"Для новой попытки бронирования: /book"
        )
        context.user_data.clear()
        return ConversationHandler.END


def build_date_buttons(day_type: str, days_ahead: int = 14) -> list:
    """
    Строит кнопки выбора даты, показывая ТОЛЬКО дни, подходящие под выбранный тип.

    Args:
        day_type: "weekday" (Пн-Пт) или "weekend" (Сб-Вс)
        days_ahead: на сколько дней вперёд показывать

    Returns:
        list: список рядов кнопок (без "Назад"/"Отмена")
    """
    buttons = []
    today = datetime.now()
    days_ru = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

    for i in range(days_ahead):
        day = today + timedelta(days=i)
        # Пропускаем даты, не соответствующие выбору пользователя
        if day_type == "weekend" and not is_weekend(day):
            continue
        if day_type == "weekday" and is_weekend(day):
            continue
        day_name = days_ru[day.weekday()]
        date_str = day.strftime(f"%d.%m {day_name}")
        date_iso = day.strftime("%Y-%m-%d")
        buttons.append([InlineKeyboardButton(date_str, callback_data=f"date_{date_iso}")])

    logger.info(f"Показано дат для day_type={day_type}: {len(buttons)}")
    return buttons


async def show_date_keyboard_message(update: Update, context: ContextTypes.DEFAULT_TYPE, bath_name: str):
    """Показывает клавиатуру выбора даты на 2 недели вперёд"""
    keyboard = build_date_buttons(context.user_data['day_type'])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_procedures")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="book_cancel")])
    markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(f"{bath_name}\nВыберите дату:", reply_markup=markup)


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

        keyboard = build_date_buttons(context.user_data['day_type'])
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_procedures")])
        keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="book_cancel")])

        await query.edit_message_text(
            f"{bath['name']}\n{day_text}\n{proc_text}\nГостей: {guest_count}\nВыберите дату:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return BOOK_DATE

    date_iso = query.data.split("_")[1]
    date_obj = datetime.strptime(date_iso, "%Y-%m-%d")

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

    slots = await get_free_slots(int(bath['staff_id']), service_id, date_iso)

    if not slots:
        logger.info(f"Нет свободных слотов для даты {date_iso}, service_id={service_id}")
        await query.edit_message_text(
            f"Баня: {bath['name']}\nДата: {date_iso}\n\nНа эту дату нет свободных слотов 😔",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Выбрать другую дату", callback_data="back_date")
            ]])
        )
        return BOOK_DATE

    keyboard = []
    for slot in slots[:12]:
        time_str = slot['time']
        keyboard.append([InlineKeyboardButton(time_str, callback_data=f"time_{time_str}")])

    keyboard.append([InlineKeyboardButton("⬅️ Другая дата", callback_data="back_date")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="book_cancel")])

    await query.edit_message_text(
        f"Баня: {bath['name']}\nДни: {day_text}\nПроцедуры: {proc_text}\nГостей: {guest_count}\nДата: {date_iso}\nСвободное время:",
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

        keyboard = build_date_buttons(context.user_data['day_type'])
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_procedures")])
        keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="book_cancel")])

        await query.edit_message_text(
            f"{bath['name']}\n{day_text}\n{proc_text}\nГостей: {guest_count}\nВыберите дату:",
            reply_markup=InlineKeyboardMarkup(keyboard)
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

    comment = f"Бронь {bath['name']} через Telegram бота\nДни: {day_text}\nПроцедуры: {proc_text}\nГостей: {guest_count}"

    success = await create_booking(
        yclients_id=int(context.user_data['yclients_id']),
        staff_id=int(bath['staff_id']),
        service_id=service_id,
        datetime_str=datetime_str,
        comment=comment,
        seance_length=seance_length
    )

    if success:
        logger.info(f"Бронирование успешно создано для пользователя {update.effective_user.id}")
        await query.edit_message_text(
            f"Готово! Вы забронированы ✅\n\n"
            f"🌿 Баня: {bath['name']}\n"
            f"📅 Дни: {day_text}\n"
            f"💆 Процедуры: {proc_text}\n"
            f"👥 Гостей: {guest_count}\n"
            f"📅 Дата: {date_iso}\n"
            f"⏰ Время: {time_str}\n"
            f"⏳ Длительность: {seance_length // 3600} ч\n\n"
            f"Нужно больше времени? Напишите или позвоните менеджеру:\n"
            f"👤 {MANAGER_NAME}\n"
            f"📱 {MANAGER_PHONE}\n\n"
            f"Ждём вас!"
        )
    else:
        logger.error(f"Не удалось создать бронирование для пользователя {update.effective_user.id}")
        await query.edit_message_text(
            "Не удалось забронировать 😔 Слот уже заняли или ошибка.\nПопробуйте /book ещё раз"
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
        await message.reply_text(
            "Отменено.\n/book — бронирование\n/start — регистрация заново",
            reply_markup=ReplyKeyboardRemove(),
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
        await message.reply_text(
            "Отменено.\n/book — бронирование\n/start — регистрация заново",
            reply_markup=ReplyKeyboardRemove(),
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


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /help"""
    await update.message.reply_text(
        "/start - регистрация\n"
        "/book - забронировать баню\n"
        "/cancel - отменить текущее действие\n"
        "/help - помощь"
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


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальный обработчик ошибок — не даёт боту падать на некритичных ошибках Telegram API"""
    if isinstance(context.error, BadRequest) and "Message is not modified" in str(context.error):
        # Пользователь нажал ту же кнопку дважды подряд — просто игнорируем
        return
    logger.error(f"Необработанная ошибка: {context.error}", exc_info=context.error)


def main() -> None:
    """запуск бота"""
    check_config()
    persistence = PicklePersistence(filepath=PERSISTENCE_FILE)
    application = Application.builder().token(BOT_TOKEN).persistence(persistence).build()
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

    application.add_handler(reg_handler, group=1)
    application.add_handler(book_handler, group=1)
    application.add_handler(CommandHandler("help", help_command), group=1)
    application.add_error_handler(error_handler)

    logger.info("Бот запущен")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
