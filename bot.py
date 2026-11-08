# -*- coding: utf-8 -*-
"""
Telegram-бот для поиска атмосферных мест севера СПб и Ленинградской области.
Поддерживает:
- Выбор точки старта (станции метро СПб: Озерки, Гражданский пр., Парнас, Беговая и др. или GPS-геолокация).
- Динамический расчет точного расстояния, времени в пути на авто и маршрута в Яндекс.Картах от выбранного метро.
- Фильтр расстояния: 20 км, 30 км, 40 км, 50 км, до 60 км.
- Подбор мест под время суток и атмосферу (Ночь, Вечер/закат, День, Утро/рассвет) с проверкой ночной доступности 24/7.
- Компактное и интуитивно понятное меню.
- Отметки «Уже был здесь» и «Избранное».
- Моментальный отклик без задержек.
"""

import os
import sys
import logging
import asyncio
from typing import List, Dict, Any, Optional

# Настройка кодировки для Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web

from places_data import (
    CATEGORY_NAMES, ZONE_NAMES, DISTANCE_GROUPS, TIME_FILTERS,
    METRO_STATIONS, DEFAULT_METRO_ID, find_nearest_metro
)
import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Инициализация базы данных и кеша
db.init_db()

# Получение токена
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    env_file = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_file):
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("BOT_TOKEN="):
                    TOKEN = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
                    break

bot = Bot(token=TOKEN) if TOKEN and TOKEN != "YOUR_TELEGRAM_BOT_TOKEN_HERE" else None
dp = Dispatcher()


# FSM для добавления собственного места (сохранено для будущих планов)
class AddPlaceForm(StatesGroup):
    waiting_title = State()
    waiting_vibe = State()
    waiting_map = State()


def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    """
    Компактная и интуитивно понятная главная клавиатура.
    Кнопки короткие, аккуратные и понятные.
    """
    kb = [
        [KeyboardButton(text="🧭 Смотреть места")],
        [KeyboardButton(text="📍 Точка старта"), KeyboardButton(text="📏 Дистанция")],
        [KeyboardButton(text="🕒 Время и вайб"), KeyboardButton(text="🌲 Направление")],
        [KeyboardButton(text="⭐ Избранное"), KeyboardButton(text="✅ Где я был")],
        [KeyboardButton(text="📊 Профиль"), KeyboardButton(text="🔁 Сброс")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


def format_card(place: Dict[str, Any]) -> str:
    """Красивая карточка спота с динамическим расчетом от выбранной точки старта."""
    cat_emoji = CATEGORY_NAMES.get(place.get("category", ""), "📍")
    zone_name = ZONE_NAMES.get(place.get("zone", ""), "Север СПб и ЛО")
    start_name = place.get("start_name", "м. Озерки")
    dist_km = place.get("distance_km", 25)
    car_time = place.get("car_time", "~25 мин")
    
    # Бейдж популярности
    pop = place.get("popularity", "hidden")
    if place.get("is_new") or pop == "new_discovery":
        badge = " ✨ <b>[Свежее открытие!]</b>"
    elif place.get("is_custom"):
        badge = " <i>[Добавлено тобой]</i>"
    elif pop == "secret":
        badge = " 🔥 <b>[Секретный спот]</b>"
    elif pop == "hidden":
        badge = " 🌲 <b>[Небанальная точка]</b>"
    else:
        badge = ""
        
    # Статус доступности 24/7
    if place.get("is_24_7", 1):
        access_badge = "🟢 <b>ВЪЕЗД:</b> Круглосуточно 24/7 (въезд открыт, шлагбаумов нет)"
    else:
        access_badge = f"🟡 <b>ВЪЕЗД:</b> {place.get('opening_hours', 'Только днем')} (ночью закрыто!)"

    # Описание ночного чилла
    night_block = ""
    if place.get("night_vibe"):
        car_icon = "🚗 " if place.get("car_chill") else "🚶‍♂️ "
        night_block = (
            f"\n\n🌙 <b>Ночной вайб (чилл / вид / у капота):</b>\n"
            f"{car_icon}{place['night_vibe']}"
        )

    text = (
        f"<b>{place['title']}</b>{badge}\n"
        f"<i>{cat_emoji} • 📍 {zone_name}</i>\n\n"
        f"⏱ <b>От {start_name}:</b> ~{dist_km} км ({car_time})\n"
        f"{access_badge}\n\n"
        f"✨ <b>Вайб и атмосфера:</b>\n{place['vibe']}"
        f"{night_block}\n\n"
        f"🚙 <b>Дорога и парковка:</b>\n{place['car_access']}\n\n"
        f"📸 <b>Идеи для фото:</b> {place['photo_tips']}\n"
    )
    return text


def get_swipe_keyboard(place_id: str, yandex_map_url: str) -> InlineKeyboardMarkup:
    """Компактные инлайн-кнопки карточки."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="❤️ В избранное", callback_data=f"like_{place_id}"),
        InlineKeyboardButton(text="➡️ Пропустить", callback_data=f"skip_{place_id}")
    )
    builder.row(
        InlineKeyboardButton(text="✅ Уже был здесь", callback_data=f"visited_{place_id}")
    )
    builder.row(
        InlineKeyboardButton(text="🗺 Маршрут в Яндекс.Картах", url=yandex_map_url)
    )
    return builder.as_markup()


# ---------------- ХЕНДЛЕРЫ МЕНЮ ----------------

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    stats = db.get_user_stats(user_id)
    
    greeting = (
        "👋 <b>Гид по атмосферным спотам СПб и Ленинградской области готов!</b>\n\n"
        f"📍 <b>Точка отсчета:</b> {stats['start_name']}\n"
        "<i>(Можно сменить в меню «📍 Точка старта» на Гражданский пр., Просвещения, Беговую или по GPS)</i>\n\n"
        "🎯 <b>Текущие настройки поиска:</b>\n"
        f"• Старт: <b>{stats['start_name']}</b>\n"
        f"• Дистанция: <b>{stats['dist_filter']}</b>\n"
        f"• Время и вайб: <b>{stats['time_filter']}</b>\n"
        f"• Направление: <b>{stats['zone_filter']}</b>\n\n"
        "💡 <b>Как это работает:</b>\n"
        "• Бот сам рассчитывает точное расстояние, время на авто и строит маршрут в Яндекс.Картах прямо от твоего метро.\n"
        "• Подбирает места, которые красивее всего именно в выбранное время (ночью — свободный въезд 24/7 и вид на огни, вечером — закат на заливе, днем — экотропы).\n"
        "• Жми «✅ Уже был здесь», чтобы исключить знакомые точки.\n\n"
        "Жми <b>«🧭 Смотреть места»</b>, чтобы начать!"
    )
    await message.answer(greeting, parse_mode="HTML", reply_markup=get_main_menu_keyboard())


# Обработка просмотра локаций
@dp.message(F.text.in_([
    "🧭 Смотреть места", "Смотреть места",
    "🧭 Смотреть локации", "Смотреть локации",
    "🧭 Найти ночной спот (до 60 км)", "🧭 Найти ночной спот (до 50 км)"
]))
async def show_feed(message: types.Message):
    """Показать следующую подходящую локацию."""
    user_id = message.from_user.id
    next_place = db.get_next_smart_place(user_id)
    
    if not next_place:
        stats = db.get_user_stats(user_id)
        await message.answer(
            f"🎉 <b>Ты просмотрел все доступные споты ({stats['dist_filter']}, {stats['time_filter']})!</b>\n\n"
            "Что можно сделать прямо сейчас:\n"
            "1. Увеличить радиус в <b>«📏 Дистанция»</b> (до 40, 50 или 60 км).\n"
            "2. Сменить атмосферу в <b>«🕒 Время и вайб»</b> (например, посмотреть вечерние закаты или дневные экотропы).\n"
            "3. Сменить точку в <b>«📍 Точка старта»</b> (например, выбрать м. Гражданский проспект).\n"
            "4. Нажать <b>«🔁 Сброс»</b>, чтобы пролистать базу заново.",
            parse_mode="HTML",
            reply_markup=get_main_menu_keyboard()
        )
        return
        
    card_text = format_card(next_place)
    kb = get_swipe_keyboard(next_place["id"], next_place["yandex_map"])
    await message.answer(card_text, parse_mode="HTML", reply_markup=kb)


# ---------------- РЕАКЦИИ: LIKE / SKIP / VISITED ----------------

@dp.callback_query(F.data.startswith("like_"))
async def handle_like(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    place_id = callback.data.split("_", 1)[1]
    
    db.record_reaction(user_id, place_id, "liked")
    await callback.answer("Сохранено в Избранное! ⭐", show_alert=False)
    
    next_place = db.get_next_smart_place(user_id)
    if not next_place:
        await callback.message.edit_text(
            "🎉 <b>Все локации в выбранном фильтре просмотрены!</b>\n\n"
            "Все сохраненные маршруты ждут тебя во вкладке <b>«⭐ Избранное»</b>.\n"
            "Попробуй увеличить радиус дистанции или сменить фильтр времени!",
            parse_mode="HTML"
        )
        return
        
    card_text = format_card(next_place)
    kb = get_swipe_keyboard(next_place["id"], next_place["yandex_map"])
    await callback.message.edit_text(card_text, parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("skip_"))
async def handle_skip(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    place_id = callback.data.split("_", 1)[1]
    
    db.record_reaction(user_id, place_id, "skipped")
    await callback.answer("Пропущено", show_alert=False)
    
    next_place = db.get_next_smart_place(user_id)
    if not next_place:
        await callback.message.edit_text(
            "🎉 <b>Ты просмотрел все локации в этом фильтре!</b>\n\n"
            "Попробуй выбрать больший радиус в меню <b>«📏 Дистанция»</b> или сменить атмосферу.",
            parse_mode="HTML"
        )
        return
        
    card_text = format_card(next_place)
    kb = get_swipe_keyboard(next_place["id"], next_place["yandex_map"])
    await callback.message.edit_text(card_text, parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data.startswith("visited_"))
async def handle_visited(callback: types.CallbackQuery):
    """Пользователь отмечает, что уже был в этом месте."""
    user_id = callback.from_user.id
    place_id = callback.data.split("_", 1)[1]
    
    db.record_reaction(user_id, place_id, "visited")
    await callback.answer("✅ Отмечено: «Был здесь»! Скрыто из ленты.", show_alert=False)
    
    next_place = db.get_next_smart_place(user_id)
    if not next_place:
        await callback.message.edit_text(
            "✅ <b>Отметка сохранена!</b>\n\n"
            "Это место добавлено в список <b>«✅ Где я был»</b> и больше никогда не появится в ленте.\n\n"
            "🎉 Все остальные доступные локации в этом фильтре тоже просмотрены!",
            parse_mode="HTML"
        )
        return
        
    card_text = format_card(next_place)
    kb = get_swipe_keyboard(next_place["id"], next_place["yandex_map"])
    await callback.message.edit_text(card_text, parse_mode="HTML", reply_markup=kb)


# ---------------- ВЫБОР ТОЧКИ СТАРТА (МЕТРО / GPS) ----------------

@dp.message(F.text.in_([
    "📍 Точка старта", "Точка старта",
    "Выбор метро", "Метро", "Точка выбора", "Откуда считать"
]))
async def select_start_point_menu(message: types.Message):
    user_id = message.from_user.id
    start_pt = db.get_user_start_point(user_id)
    
    builder = InlineKeyboardBuilder()
    
    # Станции метро попарно в ряду
    metro_keys = list(METRO_STATIONS.keys())
    for i in range(0, len(metro_keys), 2):
        row_btns = []
        for k in metro_keys[i:i+2]:
            m = METRO_STATIONS[k]
            mark = "✅ " if k == start_pt.get("id") else ""
            btn_text = f"{mark}{m['short_name']}"
            row_btns.append(InlineKeyboardButton(text=btn_text, callback_data=f"set_metro_{k}"))
        builder.row(*row_btns)
        
    await message.answer(
        f"📍 <b>Откуда считаем расстояние и маршруты?</b>\n\n"
        f"Текущая точка старта: <b>{start_pt['name']}</b> ({start_pt.get('hint', '')})\n\n"
        "Выбери ближайшую станцию метро ниже или просто <b>отправь свою геопозицию в чат</b> 📎, "
        "и бот сам определит ближайшее метро:",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


@dp.callback_query(F.data.startswith("set_metro_"))
async def save_start_metro(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    metro_id = callback.data.split("_", 2)[2]
    
    db.set_user_start_metro(user_id, metro_id)
    metro_info = METRO_STATIONS.get(metro_id, METRO_STATIONS[DEFAULT_METRO_ID])
    
    await callback.answer(f"Точка старта: {metro_info['name']}", show_alert=False)
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🧭 Смотреть места", callback_data="open_feed_direct"))
    builder.row(InlineKeyboardButton(text="📍 Сменить метро", callback_data="reopen_start_point"))
    
    await callback.message.edit_text(
        f"✅ <b>Точка старта обновлена: {metro_info['name']}!</b>\n\n"
        f"• <i>{metro_info['hint']}</i>\n\n"
        "Теперь все расстояния в км, время в пути на авто и маршруты в Яндекс.Картах "
        "будут рассчитываться прямо отсюда.\n\n"
        "Жми кнопку ниже, чтобы продолжить просмотр:",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


@dp.callback_query(F.data == "reopen_start_point")
async def reopen_start_point_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    start_pt = db.get_user_start_point(user_id)
    
    builder = InlineKeyboardBuilder()
    metro_keys = list(METRO_STATIONS.keys())
    for i in range(0, len(metro_keys), 2):
        row_btns = []
        for k in metro_keys[i:i+2]:
            m = METRO_STATIONS[k]
            mark = "✅ " if k == start_pt.get("id") else ""
            btn_text = f"{mark}{m['short_name']}"
            row_btns.append(InlineKeyboardButton(text=btn_text, callback_data=f"set_metro_{k}"))
        builder.row(*row_btns)
        
    await callback.message.edit_text(
        f"📍 <b>Выбери станцию метро для отсчета:</b>\n"
        f"Текущая точка: <b>{start_pt['name']}</b>",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


# Обработка отправки геопозиции
@dp.message(F.location)
async def handle_user_location(message: types.Message):
    """Пользователь отправил геопозицию: находим ближайшее метро."""
    user_id = message.from_user.id
    lat = message.location.latitude
    lon = message.location.longitude
    
    nearest_id, nearest_metro, dist_km = find_nearest_metro(lat, lon)
    db.set_user_start_metro(user_id, nearest_id)
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🧭 Смотреть места", callback_data="open_feed_direct"))
    builder.row(InlineKeyboardButton(text="📍 Выбрать другое метро", callback_data="reopen_start_point"))
    
    await message.answer(
        f"🎯 <b>Геопозиция успешно определена!</b>\n\n"
        f"Ближайшая станция к тебе: <b>{nearest_metro['name']}</b> (~{dist_km} км).\n"
        f"Она установлена как твоя точка старта для расчета расстояний и навигации!\n\n"
        "Жми <b>«🧭 Смотреть места»</b>, чтобы искать споты рядом с тобой:",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


# ---------------- ФИЛЬТР РАССТОЯНИЯ ----------------

@dp.message(F.text.in_([
    "📏 Дистанция", "Дистанция",
    "📏 Дистанция (до 60 км)", "📏 Дистанция (до 50 км)",
    "Фильтр расстояния", "Радиус"
]))
async def change_distance_filter(message: types.Message):
    user_id = message.from_user.id
    current_dist = db.get_user_distance_filter(user_id)
    start_pt = db.get_user_start_point(user_id)
    
    builder = InlineKeyboardBuilder()
    for key, label in DISTANCE_GROUPS.items():
        mark = "✅ " if key == current_dist else ""
        builder.row(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"set_dist_{key}"))
        
    await message.answer(
        f"📏 <b>Выбери максимальную дистанцию (от {start_pt['name']}):</b>\n\n"
        "• <b>До 20 км</b> — быстрый спот на 15–25 минут\n"
        "• <b>До 30 км</b> — ближний пригород и озера (~30 мин)\n"
        "• <b>До 40 км</b> — побережье, Дамба и холмы (~40 мин)\n"
        "• <b>До 50 км</b> — дальний берег и Ладога (~50 мин)\n"
        "• <b>До 60 км</b> — максимальный выезд (~1 час)",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


@dp.callback_query(F.data.startswith("set_dist_"))
async def save_distance_filter(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    key = callback.data.split("_", 2)[2]
    
    db.set_user_distance_filter(user_id, key)
    dist_name = DISTANCE_GROUPS.get(key, f"До {key} км")
    start_pt = db.get_user_start_point(user_id)
    
    await callback.answer(f"Дистанция: {dist_name}", show_alert=False)
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🧭 Смотреть места", callback_data="open_feed_direct"))
    
    await callback.message.edit_text(
        f"✅ <b>Установлен радиус:</b> {dist_name}\n"
        f"Точка отсчета: <b>{start_pt['name']}</b>.\n\n"
        "Локации дальше этой дистанции показываться не будут.",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


# ---------------- ФИЛЬТР: ВРЕМЯ И ВАЙБ (ПОДБОР ПО КРАСОТЕ И ДОСТУПНОСТИ) ----------------

@dp.message(F.text.in_([
    "🕒 Время и вайб", "Время и вайб",
    "🌙 Ночной фильтр (24/7)", "🌙 Ночной фильтр", "Ночной фильтр",
    "Время суток", "Выбор времени"
]))
async def select_time_filter(message: types.Message):
    user_id = message.from_user.id
    current_filter = db.get_user_time_filter(user_id)
    
    builder = InlineKeyboardBuilder()
    for key, label in TIME_FILTERS.items():
        mark = "✅ " if key == current_filter else ""
        builder.row(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"set_time_{key}"))
        
    await message.answer(
        "🕒 <b>Выбери атмосферу и время поездки:</b>\n\n"
        "Бот подбирает локации, которые <b>особенно красивы именно в это время</b>, "
        "и обязательно проверяет их доступность:\n\n"
        "• 🌙 <b>Ночь</b> — вид на огни ночного города, Лахта-Центр, звездное небо над водой, "
        "авто-чилл у капота и <b>строго круглосуточный свободный въезд 24/7</b> (без закрытых ворот и шлагбаумов).\n"
        "• 🌅 <b>Вечер и закат</b> — золотой час, закатные панорамы Финского залива и озер.\n"
        "• ☀️ <b>День</b> — сосновые экотропы, песчаные карьеры, форты и прогулки.\n"
        "• 🌄 <b>Утро и рассвет</b> — зеркальная гладь озер в тумане, восход солнца над Ладогой, тишина без людей.\n"
        "• 🌐 <b>Любое время</b> — весь каталог без ограничений.",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


@dp.callback_query(F.data.startswith("set_time_"))
async def save_time_filter(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    key = callback.data.split("_", 2)[2]
    
    db.set_user_time_filter(user_id, key)
    time_name = TIME_FILTERS.get(key, "Любое время")
    
    await callback.answer(f"Режим: {time_name}", show_alert=False)
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🧭 Смотреть места", callback_data="open_feed_direct"))
    
    await callback.message.edit_text(
        f"✅ <b>Выбран режим:</b> {time_name}\n\n"
        "Теперь в ленте будут места, идеально подходящие под эту атмосферу!",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


# ---------------- ВЫБОР НАПРАВЛЕНИЯ / ЗОНЫ ----------------

@dp.message(F.text.in_([
    "🌲 Направление", "Направление",
    "📍 Выбор зоны", "Выбор зоны", "Зона"
]))
async def select_zone_filter(message: types.Message):
    user_id = message.from_user.id
    current_zone = db.get_user_zone_filter(user_id)
    
    builder = InlineKeyboardBuilder()
    for key, label in ZONE_NAMES.items():
        mark = "✅ " if key == current_zone else ""
        builder.row(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"set_zone_{key}"))
        
    await message.answer(
        "🌲 <b>Выбери географическое направление:</b>\n\n"
        "• <b>Весь Север</b> — свободный поиск по всем направлениям.\n"
        "• <b>Курортный р-н</b> — берег залива от Сестрорецка до Зеленогорска.\n"
        "• <b>Озера и Перешеек</b> — Токсово, Рощино, Медное озеро, холмы.\n"
        "• <b>Север СПб</b> — Озерки, Шувалово, Юкки, Лахта, Крестовский.\n"
        "• <b>Дамба и Кронштадт</b> — молы посреди моря, форты.\n"
        "• <b>Всеволожск и Ладога</b> — пляжи Ладожского озера и сосновые леса.",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


@dp.callback_query(F.data.startswith("set_zone_"))
async def save_zone_filter(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    key = callback.data.split("_", 2)[2]
    
    db.set_user_zone_filter(user_id, key)
    zone_name = ZONE_NAMES.get(key, "Весь Север СПб и ЛО")
    
    await callback.answer(f"Направление: {zone_name}", show_alert=False)
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🧭 Смотреть места", callback_data="open_feed_direct"))
    
    await callback.message.edit_text(
        f"✅ <b>Выбрано направление:</b> {zone_name}\n\n"
        "Жми кнопку ниже, чтобы начать просмотр!",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


# Быстрый переход к ленте по инлайн-кнопке
@dp.callback_query(F.data == "open_feed_direct")
async def open_feed_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    next_place = db.get_next_smart_place(user_id)
    
    if not next_place:
        stats = db.get_user_stats(user_id)
        await callback.message.edit_text(
            f"🎉 <b>Все локации в этом фильтре просмотрены ({stats['dist_filter']}, {stats['time_filter']})!</b>\n\n"
            "Попробуй увеличить радиус или сменить режим времени в меню.",
            parse_mode="HTML"
        )
        return
        
    card_text = format_card(next_place)
    kb = get_swipe_keyboard(next_place["id"], next_place["yandex_map"])
    await callback.message.edit_text(card_text, parse_mode="HTML", reply_markup=kb)


# ---------------- ИЗБРАННОЕ ----------------

@dp.message(F.text.in_(["⭐ Избранное", "Избранное"]))
async def show_favorites(message: types.Message):
    user_id = message.from_user.id
    favorites = db.get_user_favorites(user_id)
    
    if not favorites:
        await message.answer(
            "⭐ <b>Твое избранное пока пусто!</b>\n\n"
            "Нажми <b>«🧭 Смотреть места»</b> и ставь <b>❤️ В избранное</b> на понравившихся точках.",
            parse_mode="HTML",
            reply_markup=get_main_menu_keyboard()
        )
        return
        
    builder = InlineKeyboardBuilder()
    for place in favorites:
        btn_text = f"📍 {place['title']} (~{place['distance_km']} км)"
        builder.row(InlineKeyboardButton(text=btn_text, callback_data=f"fav_view_{place['id']}"))
        
    await message.answer(
        f"⭐ <b>Твои сохраненные локации ({len(favorites)}):</b>\n"
        "Нажми на любое место, чтобы открыть карточку и маршрут:",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


@dp.callback_query(F.data.startswith("fav_view_"))
async def view_favorite_place(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    place_id = callback.data.split("_", 2)[2]
    
    # Ищем место и обогащаем от точки пользователя
    start_pt = db.get_user_start_point(user_id)
    place = db.get_place_by_id(place_id)
    
    if not place:
        await callback.answer("Локация не найдена!", show_alert=True)
        return
        
    d_km = db.haversine_distance_km(start_pt["lat"], start_pt["lon"], place.get("lat", 60.0), place.get("lon", 30.0))
    place["distance_km"] = d_km
    place["car_time"] = db.estimate_car_time(d_km)
    place["start_name"] = start_pt["name"]
    route_url = f"https://yandex.ru/maps/?rtext={start_pt['lat']},{start_pt['lon']}~{place.get('lat', 60.0)},{place.get('lon', 30.0)}&rtt=auto"
    place["yandex_map"] = route_url
    
    card_text = format_card(place)
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🗺 Маршрут в Яндекс.Картах", url=route_url))
    builder.row(
        InlineKeyboardButton(text="✅ Уже побывал тут", callback_data=f"fav_visited_{place['id']}"),
        InlineKeyboardButton(text="❌ Из избранного", callback_data=f"fav_del_{place['id']}")
    )
    builder.row(InlineKeyboardButton(text="⬅️ Назад в Избранное", callback_data="fav_back_list"))
    
    await callback.message.edit_text(card_text, parse_mode="HTML", reply_markup=builder.as_markup())


@dp.callback_query(F.data.startswith("fav_visited_"))
async def favorite_mark_visited(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    place_id = callback.data.split("_", 2)[2]
    
    db.record_reaction(user_id, place_id, "visited")
    await callback.answer("Отмечено как посещенное! Перенесено в «✅ Где я был».", show_alert=True)
    
    favorites = db.get_user_favorites(user_id)
    if not favorites:
        await callback.message.edit_text("⭐ В избранном больше ничего нет.", parse_mode="HTML")
        return
        
    builder = InlineKeyboardBuilder()
    for place in favorites:
        btn_text = f"📍 {place['title']} (~{place['distance_km']} км)"
        builder.row(InlineKeyboardButton(text=btn_text, callback_data=f"fav_view_{place['id']}"))
        
    await callback.message.edit_text(
        f"⭐ <b>Твои сохраненные локации ({len(favorites)}):</b>",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


@dp.callback_query(F.data.startswith("fav_del_"))
async def delete_favorite_place(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    place_id = callback.data.split("_", 2)[2]
    
    db.remove_favorite(user_id, place_id)
    await callback.answer("Удалено из избранного!", show_alert=True)
    
    favorites = db.get_user_favorites(user_id)
    if not favorites:
        await callback.message.edit_text("⭐ В избранном больше ничего нет.", parse_mode="HTML")
        return
        
    builder = InlineKeyboardBuilder()
    for place in favorites:
        btn_text = f"📍 {place['title']} (~{place['distance_km']} км)"
        builder.row(InlineKeyboardButton(text=btn_text, callback_data=f"fav_view_{place['id']}"))
        
    await callback.message.edit_text(
        f"⭐ <b>Твои сохраненные локации ({len(favorites)}):</b>",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


@dp.callback_query(F.data == "fav_back_list")
async def back_to_fav_list(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    favorites = db.get_user_favorites(user_id)
    
    if not favorites:
        await callback.message.edit_text("⭐ Твое избранное пусто.", parse_mode="HTML")
        return
        
    builder = InlineKeyboardBuilder()
    for place in favorites:
        btn_text = f"📍 {place['title']} (~{place['distance_km']} км)"
        builder.row(InlineKeyboardButton(text=btn_text, callback_data=f"fav_view_{place['id']}"))
        
    await callback.message.edit_text(
        f"⭐ <b>Твои сохраненные локации ({len(favorites)}):</b>",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


# ---------------- РАЗДЕЛ: ГДЕ Я БЫЛ ----------------

@dp.message(F.text.in_(["✅ Где я был", "Где я был", "Посещенные места"]))
async def show_visited_places(message: types.Message):
    user_id = message.from_user.id
    visited = db.get_user_visited(user_id)
    
    if not visited:
        await message.answer(
            "✅ <b>Список посещенных мест пока пуст!</b>\n\n"
            "Когда листаешь споты (<b>«🧭 Смотреть места»</b>), "
            "нажимай <b>«✅ Уже был здесь»</b> на знакомых точках. "
            "Бот запомнит их и больше не станет предлагать в ленте.",
            parse_mode="HTML",
            reply_markup=get_main_menu_keyboard()
        )
        return
        
    builder = InlineKeyboardBuilder()
    for place in visited:
        btn_text = f"✅ {place['title']} (~{place['distance_km']} км)"
        builder.row(InlineKeyboardButton(text=btn_text, callback_data=f"vis_view_{place['id']}"))
        
    await message.answer(
        f"✅ <b>Места, где ты уже побывал ({len(visited)}):</b>\n"
        "Они исключены из ленты рекомендаций. Нажми, чтобы открыть карточку или вернуть место в ленту:",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


@dp.callback_query(F.data.startswith("vis_view_"))
async def view_visited_place(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    place_id = callback.data.split("_", 2)[2]
    
    start_pt = db.get_user_start_point(user_id)
    place = db.get_place_by_id(place_id)
    
    if not place:
        await callback.answer("Локация не найдена!", show_alert=True)
        return
        
    d_km = db.haversine_distance_km(start_pt["lat"], start_pt["lon"], place.get("lat", 60.0), place.get("lon", 30.0))
    place["distance_km"] = d_km
    place["car_time"] = db.estimate_car_time(d_km)
    place["start_name"] = start_pt["name"]
    route_url = f"https://yandex.ru/maps/?rtext={start_pt['lat']},{start_pt['lon']}~{place.get('lat', 60.0)},{place.get('lon', 30.0)}&rtt=auto"
    place["yandex_map"] = route_url
    
    card_text = format_card(place)
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🗺 Маршрут в Яндекс.Картах", url=route_url))
    builder.row(InlineKeyboardButton(text="❌ Вернуть место в ленту", callback_data=f"vis_del_{place['id']}"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад к посещенным", callback_data="vis_back_list"))
    
    await callback.message.edit_text(card_text, parse_mode="HTML", reply_markup=builder.as_markup())


@dp.callback_query(F.data.startswith("vis_del_"))
async def delete_visited_mark(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    place_id = callback.data.split("_", 2)[2]
    
    db.remove_visited(user_id, place_id)
    await callback.answer("Отметка снята! Место снова может появиться в ленте.", show_alert=True)
    
    visited = db.get_user_visited(user_id)
    if not visited:
        await callback.message.edit_text(
            "✅ <b>В списке посещенных больше ничего нет.</b>\n"
            "Все точки снова доступны к показу в ленте!",
            parse_mode="HTML"
        )
        return
        
    builder = InlineKeyboardBuilder()
    for place in visited:
        btn_text = f"✅ {place['title']} (~{place['distance_km']} км)"
        builder.row(InlineKeyboardButton(text=btn_text, callback_data=f"vis_view_{place['id']}"))
        
    await callback.message.edit_text(
        f"✅ <b>Места, где ты уже побывал ({len(visited)}):</b>",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


@dp.callback_query(F.data == "vis_back_list")
async def back_to_visited_list(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    visited = db.get_user_visited(user_id)
    
    if not visited:
        await callback.message.edit_text("✅ Список посещенных мест пуст.", parse_mode="HTML")
        return
        
    builder = InlineKeyboardBuilder()
    for place in visited:
        btn_text = f"✅ {place['title']} (~{place['distance_km']} км)"
        builder.row(InlineKeyboardButton(text=btn_text, callback_data=f"vis_view_{place['id']}"))
        
    await callback.message.edit_text(
        f"✅ <b>Места, где ты уже побывал ({len(visited)}):</b>",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


# ---------------- СТАТИСТИКА И ПРОФИЛЬ ----------------

@dp.message(F.text.in_(["📊 Профиль", "Профиль", "📊 Мой профиль", "Мой профиль"]))
async def user_profile(message: types.Message):
    """
    Профиль пользователя.
    Гарантированно возвращает новое компактное меню (исправлен баг со старым меню).
    """
    user_id = message.from_user.id
    stats = db.get_user_stats(user_id)
    
    fav_pref = stats["top_category"] if stats["top_category"] else "еще формируется"
    disc_info = db.get_discovery_status()
    
    profile_text = (
        "📊 <b>Твой профиль путешественника:</b>\n\n"
        f"👁 <b>Просмотрено спотов:</b> {stats['total_seen']} из {stats['total_available']}\n"
        f"❤️ <b>В избранном:</b> {stats['liked_count']}\n"
        f"✅ <b>Уже побывал:</b> {stats['visited_count']}\n"
        f"➡️ <b>Пропущено:</b> {stats['skipped_count']}\n\n"
        f"📍 <b>Точка старта:</b> {stats['start_name']}\n"
        f"📏 <b>Радиус поиска:</b> {stats['dist_filter']}\n"
        f"🕒 <b>Режим атмосферы:</b> {stats['time_filter']}\n"
        f"🌲 <b>Направление:</b> {stats['zone_filter']}\n"
        f"✨ <b>Любимый вайб:</b> {fav_pref}\n\n"
        f"🔄 <b>Авто-поиск новых мест:</b> каждые ~12 ч (следующий через ~{disc_info['next_in_hours']} ч)\n\n"
        "<i>Используй кнопки ниже для быстрой настройки параметров:</i>"
    )
    
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📍 Сменить метро", callback_data="reopen_start_point"),
        InlineKeyboardButton(text="📏 Дистанция", callback_data="prof_set_dist")
    )
    builder.row(
        InlineKeyboardButton(text="🕒 Время и вайб", callback_data="prof_set_time"),
        InlineKeyboardButton(text="🌲 Направление", callback_data="prof_set_zone")
    )
    
    await message.answer(
        profile_text,
        parse_mode="HTML",
        reply_markup=get_main_menu_keyboard()
    )
    await message.answer("Быстрые настройки фильтра:", reply_markup=builder.as_markup())


@dp.callback_query(F.data == "prof_set_dist")
async def profile_set_dist(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    current_dist = db.get_user_distance_filter(user_id)
    
    builder = InlineKeyboardBuilder()
    for key, label in DISTANCE_GROUPS.items():
        mark = "✅ " if key == current_dist else ""
        builder.row(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"set_dist_{key}"))
        
    await callback.message.edit_text("📏 <b>Выбери максимальную дистанцию:</b>", parse_mode="HTML", reply_markup=builder.as_markup())


@dp.callback_query(F.data == "prof_set_time")
async def profile_set_time(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    current_time = db.get_user_time_filter(user_id)
    
    builder = InlineKeyboardBuilder()
    for key, label in TIME_FILTERS.items():
        mark = "✅ " if key == current_time else ""
        builder.row(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"set_time_{key}"))
        
    await callback.message.edit_text("🕒 <b>Выбери атмосферу и время суток:</b>", parse_mode="HTML", reply_markup=builder.as_markup())


@dp.callback_query(F.data == "prof_set_zone")
async def profile_set_zone(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    current_zone = db.get_user_zone_filter(user_id)
    
    builder = InlineKeyboardBuilder()
    for key, label in ZONE_NAMES.items():
        mark = "✅ " if key == current_zone else ""
        builder.row(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"set_zone_{key}"))
        
    await callback.message.edit_text("🌲 <b>Выбери направление:</b>", parse_mode="HTML", reply_markup=builder.as_markup())


# ---------------- СБРОС ИСТОРИИ ----------------

@dp.message(F.text.in_([
    "🔁 Сброс", "Сброс",
    "🔁 Сбросить всё", "🔁 Сбросить историю", "Сбросить историю", "Сбросить всё"
]))
async def reset_history_prompt(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="⚠️ Да, сбросить историю", callback_data="confirm_reset"),
        InlineKeyboardButton(text="Отмена", callback_data="cancel_reset")
    )
    await message.answer(
        "⚠️ <b>Ты точно хочешь сбросить историю просмотров?</b>\n\n"
        "Это очистит просмотренные карточки, чтобы ты мог пролистать базу заново. "
        "Твоя точка старта и настройки сохранятся.",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


@dp.callback_query(F.data == "confirm_reset")
async def confirm_reset_history(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    db.reset_user_history(user_id)
    
    await callback.answer("История сброшена!", show_alert=True)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🧭 Смотреть места", callback_data="open_feed_direct"))
    
    await callback.message.edit_text(
        "🔄 <b>История успешно очищена!</b>\n\n"
        "Все локации снова доступны к показу. Жми кнопку ниже:",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


@dp.callback_query(F.data == "cancel_reset")
async def cancel_reset_history(callback: types.CallbackQuery):
    await callback.message.delete()


# ---------------- ДОБАВЛЕНИЕ СВОЕГО МЕСТА (ЗАГОТОВКА ПОД ПЛАНЫ) ----------------

@dp.message(Command("add_place"))
async def start_add_place(message: types.Message, state: FSMContext):
    """Команда /add_place доступна как скрытая заготовка под планы."""
    await state.set_state(AddPlaceForm.waiting_title)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Отмена", callback_data="cancel_add_place"))
    
    await message.answer(
        "📍 <b>Добавление новой секретной точки</b>\n\n"
        "Шаг 1 из 3: Введи <b>название места</b>:",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


@dp.callback_query(F.data == "cancel_add_place")
async def cancel_add_place_handler(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Добавление места отменено.", parse_mode="HTML")


@dp.message(AddPlaceForm.waiting_title)
async def process_place_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(AddPlaceForm.waiting_vibe)
    await message.answer(
        "✨ Шаг 2 из 3: Опиши <b>атмосферу и вайб</b> (почему круто, вид на воду/огни, можно ли постоять на машине):",
        parse_mode="HTML"
    )


@dp.message(AddPlaceForm.waiting_vibe)
async def process_place_vibe(message: types.Message, state: FSMContext):
    await state.update_data(vibe=message.text.strip())
    await state.set_state(AddPlaceForm.waiting_map)
    await message.answer(
        "🗺 Шаг 3 из 3: Отправь <b>ссылку на Яндекс.Карты</b> или ориентир:",
        parse_mode="HTML"
    )


@dp.message(AddPlaceForm.waiting_map)
async def process_place_map(message: types.Message, state: FSMContext):
    map_link = message.text.strip()
    data = await state.get_data()
    await state.clear()
    
    import time
    place_id = f"custom_{int(time.time())}"
    
    new_place = {
        "id": place_id,
        "title": data["title"],
        "category": "night_chill",
        "zone": "kurortny",
        "distance_km": 25,
        "car_time": "25-30 мин",
        "range_group": "30",
        "is_24_7": 1,
        "opening_hours": "Круглосуточно 24/7",
        "night_vibe": data["vibe"],
        "car_chill": 1,
        "popularity": "secret",
        "vibe": data["vibe"],
        "season": "Круглый год",
        "time_of_day": "Круглосуточно / Ночь",
        "car_access": "Свободный подъезд на авто",
        "photo_tips": "Красивые панорамы",
        "yandex_map": map_link if map_link.startswith("http") else f"https://yandex.ru/maps/?text={map_link}"
    }
    
    success = db.add_custom_place(new_place)
    if success:
        await message.answer(
            f"🎉 <b>Локация «{data['title']}» успешно сохранена!</b>\n\n"
            "Она сразу стала доступна в каталоге рекомендаций.",
            parse_mode="HTML",
            reply_markup=get_main_menu_keyboard()
        )
    else:
        await message.answer(
            "⚠️ Ошибка при сохранении.",
            reply_markup=get_main_menu_keyboard()
        )


# ---------------- РУЧНОЙ ЗАПУСК ПОИСКА И АНАЛИЗА НОВЫХ МЕСТ ----------------

@dp.message(Command("update_now", "scan"))
async def manual_update_now(message: types.Message):
    """Принудительный запуск поиска и анализа новых локаций."""
    status_msg = await message.answer(
        "🔍 <b>Запущен интеллектуальный поиск и анализ новых локаций...</b>\n\n"
        "• Проверка координат и радиуса (до 60 км)\n"
        "• Фильтрация дубликатов (мин. 600 м от существующих точек)\n"
        "• Анализ 24/7 доступности и подъезда на авто\n"
        "• Оценка эстетики (вода, лес, закат, ночные огни)\n\n"
        "<i>Старые локации и история полностью сохраняются.</i>",
        parse_mode="HTML"
    )
    
    added, total, titles = db.auto_update_and_expand_places(force=True, batch_limit=3)
    
    if added > 0:
        titles_str = "\n".join(f"• <b>{t}</b>" for t in titles)
        await status_msg.edit_text(
            f"🎉 <b>Анализ завершен! Найдено и добавлено {added} новых локаций:</b>\n\n"
            f"{titles_str}\n\n"
            f"Всего в активной базе: <b>{total}</b> спотов.\n"
            "Они уже доступны в общей ленте с отметкой ✨ [Свежее открытие!]!",
            parse_mode="HTML"
        )
    else:
        await status_msg.edit_text(
            f"✅ <b>База актуальна (всего {total} проверенных мест)!</b>\n\n"
            "Все найденные кандидаты уже присутствуют в базе либо отклонены "
            "(слишком близко к существующим точкам или дальше 60 км). "
            "Старые места сохранены в неизменном виде.",
            parse_mode="HTML"
        )


# ---------------- ФОНОВЫЙ ПЛАНИРОВЩИК (КАЖДЫЕ ~12 ЧАСОВ) ----------------

async def background_auto_updater():
    """
    Фоновый планировщик авто-поиска локаций.
    Проверяет расписание каждые 5 минут и выполняет поиск каждые ~12 часов (±30 мин).
    Старые локации никогда не удаляются, база защищена от спама.
    """
    logger.info("[Фоновый планировщик] Запущен (цикл авто-поиска: каждые ~12 часов).")
    
    # Небольшая задержка перед первой проверкой при старте
    await asyncio.sleep(10)
    
    while True:
        try:
            added, total, titles = db.auto_update_and_expand_places(force=False, batch_limit=3)
            if added > 0:
                logger.info(f"[Авто-поиск 12ч] Добавлено {added} новых мест: {titles}. Всего в базе: {total}")
            # Проверяем каждые 5 минут
            await asyncio.sleep(300)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[Фоновый планировщик] Ошибка: {e}")
            await asyncio.sleep(600)


async def start_health_check_server():
    """Веб-сервер для поддержания работы 24/7 на облачных хостингах."""
    app = web.Application()
    async def health_handler(request):
        return web.Response(text="SPB Places Bot is running 24/7!")
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    
    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    try:
        await site.start()
        logger.info(f"Health-check сервер запущен на порту {port}")
    except Exception as e:
        logger.warning(f"Health-check сервер не запустился на порту {port}: {e}")


# ---------------- ТОЧКА ВХОДА ----------------

async def main():
    if not bot:
        print("[!] Ошибка: Не указан токен бота в .env или BOT_TOKEN!")
        sys.exit(1)
        
    print("=" * 60)
    print("[+] Бот атмосферных локаций СПб и ЛО запущен!")
    print("[+] Выбор точки старта: Озерки, Гражданский пр., Парнас, Беговая и др. + GPS.")
    print("[+] Динамический расчет расстояния, времени в пути и навигации Яндекс.Карт.")
    print("[+] Атмосферный подбор времени суток: Ночь 24/7, Закат, День, Утро.")
    print("[+] Компактное и интуитивно понятное меню.")
    print("=" * 60)
    
    # Запуск фонового health-check сервера
    await start_health_check_server()
    
    # Запуск фонового авто-обновления базы
    asyncio.create_task(background_auto_updater())
    
    # Сбрасываем старые зависшие обновления для моментального отклика
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logger.warning(f"Не удалось удалить вебхук: {e}")
        
    # Запуск polling телеграма
    await dp.start_polling(bot, drop_pending_updates=True, polling_timeout=20)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\n[+] Бот остановлен.")
