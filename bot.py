# -*- coding: utf-8 -*-
"""
Telegram-бот для поиска атмосферных мест севера СПб и Ленинградской области.
Старт: м. Озерки (Выборгское / Приморское шоссе, Скандинавия, Сортавала, КАД).
Радиус: настраиваемый (20, 30, 40, 50, до 60 км).
Особенности:
- Огромная база (100+ спотов) с фокусом на ночные локации 24/7.
- Автоматическое фоновое обновление и поиск новых мест каждые 6 часов.
- Выбор дистанции: 20 км -> 30 км -> 40 км -> 50 км -> до 60 км.
- Спец-описание для ночного чилла: вид из машины, где постоять, покурить, отдохнуть.
- Выбор зон: Курортный р-н, Дамба, Токсово/озера, Север СПб, Ладога.
- Отметка «✅ Уже был здесь» (навсегда исключает место из показа).
"""

import os
import sys
import logging
import asyncio
from typing import List, Dict, Any

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

from places_data import CATEGORY_NAMES, ZONE_NAMES, DISTANCE_GROUPS, TIME_FILTERS
import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Инициализация базы данных
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


# FSM для добавления собственного места
class AddPlaceForm(StatesGroup):
    waiting_title = State()
    waiting_vibe = State()
    waiting_map = State()


def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Главная компактная клавиатура бота (без лишней кнопки обновления, бот обновляется сам в фоне)."""
    kb = [
        [KeyboardButton(text="🧭 Смотреть локации")],
        [KeyboardButton(text="🌙 Ночной фильтр (24/7)"), KeyboardButton(text="📏 Дистанция (до 60 км)")],
        [KeyboardButton(text="📍 Выбор зоны"), KeyboardButton(text="✅ Где я был")],
        [KeyboardButton(text="⭐ Избранное"), KeyboardButton(text="📊 Мой профиль")],
        [KeyboardButton(text="➕ Добавить место"), KeyboardButton(text="🔁 Сбросить всё")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


def format_card(place: Dict[str, Any]) -> str:
    """Красивая карточка с акцентом на ночной чилл, координаты и доступность 24/7."""
    cat_emoji = CATEGORY_NAMES.get(place.get("category", ""), "📍")
    zone_name = ZONE_NAMES.get(place.get("zone", ""), "Север СПб и ЛО")
    
    # Бейдж популярности
    pop = place.get("popularity", "hidden")
    if place.get("is_custom"):
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
        access_badge = f"🔴 <b>ВЪЕЗД:</b> {place.get('opening_hours', 'Только днем')} (ночью закрыто!)"

    # Описание ночного чилла
    night_block = ""
    if place.get("night_vibe"):
        car_icon = "🚗 " if place.get("car_chill") else "🚶‍♂️ "
        night_block = (
            f"\n\n🌙 <b>Ночной вайб (чилл / покурить / вид):</b>\n"
            f"{car_icon}{place['night_vibe']}"
        )

    text = (
        f"<b>{place['title']}</b>{badge}\n"
        f"<i>{cat_emoji} • 📍 {zone_name}</i>\n\n"
        f"⏱ <b>От м. Озерки:</b> ~{place['distance_km']} км ({place['car_time']})\n"
        f"{access_badge}\n\n"
        f"✨ <b>Вайб и атмосфера:</b>\n{place['vibe']}"
        f"{night_block}\n\n"
        f"🚙 <b>Дорога и парковка:</b>\n{place['car_access']}\n\n"
        f"📸 <b>Идеи для фото:</b> {place['photo_tips']}\n"
    )
    return text


def get_swipe_keyboard(place_id: str, yandex_map_url: str) -> InlineKeyboardMarkup:
    """Инлайн-кнопки карточки: Like, Skip, Visited, Route."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="❤️ В избранное", callback_data=f"like_{place_id}"),
        InlineKeyboardButton(text="➡️ Пропустить", callback_data=f"skip_{place_id}")
    )
    builder.row(
        InlineKeyboardButton(text="✅ Уже был здесь (больше не показывать)", callback_data=f"visited_{place_id}")
    )
    builder.row(
        InlineKeyboardButton(text="🗺 Маршрут в Яндекс.Картах", url=yandex_map_url)
    )
    return builder.as_markup()


# ---------------- ХЕНДЛЕРЫ МЕНЮ ----------------

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    dist_val = db.get_user_distance_filter(user_id)
    dist_name = DISTANCE_GROUPS.get(dist_val, f"До {dist_val} км")
    time_val = db.get_user_time_filter(user_id)
    time_name = TIME_FILTERS.get(time_val, "Ночные споты 24/7")
    zone_val = db.get_user_zone_filter(user_id)
    zone_name = ZONE_NAMES.get(zone_val, "Весь Север СПб и ЛО")
    
    greeting = (
        "👋 <b>Гид по ночным и атмосферным спотам севера СПб и ЛО готов!</b>\n\n"
        "📍 <b>Точка старта: м. «Озерки»</b> (Выборгское/Приморское ш., «Скандинавия», «Сортавала», КАД и Дамба).\n"
        "🗺 В каталоге собрано <b>более 100 проверенных локаций</b>.\n\n"
        "🌙 <b>Что настроено для ночных поездок:</b>\n"
        "• <b>Ночной фильтр 24/7 по умолчанию:</b> бот сразу выдает только места со свободным въездом на машине без ворот и шлагбаумов.\n"
        "• <b>Авто-чилл:</b> подробное описание, где встать на машине, какой вид на воду/огни и можно ли постоять у капота с кофе или покурить.\n"
        "• <b>Выбор дистанции:</b> шаг 20 км -> 30 км -> 40 км -> 50 км -> до 60 км.\n"
        "• <b>Кнопка «✅ Уже был здесь»:</b> жми на знакомых местах, и бот навсегда скроет их из ленты.\n"
        "• <b>Авто-обновление:</b> база сама обновляется и пополняется каждые 6 часов в фоновом режиме.\n\n"
        f"🎯 <b>Текущие настройки:</b>\n"
        f"• Режим: <b>{time_name}</b>\n"
        f"• Дистанция: <b>{dist_name}</b>\n"
        f"• Направление: <b>{zone_name}</b>\n\n"
        "Жми <b>«🧭 Смотреть локации»</b>, чтобы начать выбор спота!"
    )
    await message.answer(greeting, parse_mode="HTML", reply_markup=get_main_menu_keyboard())


# Обработка просмотра локаций
@dp.message(F.text.in_(["🧭 Смотреть локации", "Смотреть локации", "🧭 Найти ночной спот (до 60 км)", "🧭 Найти ночной спот (до 50 км)"]))
async def show_feed(message: types.Message):
    """Показать следующую подходящую локацию."""
    user_id = message.from_user.id
    next_place = db.get_next_smart_place(user_id)
    
    if not next_place:
        dist_val = db.get_user_distance_filter(user_id)
        dist_name = DISTANCE_GROUPS.get(dist_val, f"до {dist_val} км")
        time_val = db.get_user_time_filter(user_id)
        time_name = TIME_FILTERS.get(time_val, "выбранном режиме")
        
        await message.answer(
            f"🎉 <b>Ты просмотрел все доступные споты ({dist_name}, {time_name})!</b>\n\n"
            "Что можно сделать прямо сейчас:\n"
            "1. Увеличить радиус в <b>«📏 Дистанция»</b> (например, выбрать до 40, 50 или 60 км).\n"
            "2. Сменить <b>«📍 Зону»</b> или <b>«🌙 Ночной фильтр»</b> (например, посмотреть закатные споты).\n"
            "3. Заглянуть в <b>«✅ Где я был»</b> или <b>«⭐ Избранное»</b>.\n"
            "4. Нажать <b>«🔁 Сбросить всё»</b>, чтобы пролистать базу заново.\n\n"
            "<i>База автоматически пополняется новыми точками каждые 6 часов.</i>",
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
            "Попробуй увеличить радиус дистанции или сменить зону!",
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
            "Попробуй выбрать больший радиус (например, до 50 или 60 км) в меню <b>«📏 Дистанция»</b>.",
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
    await callback.answer("✅ Отмечено: «Был здесь»! Место скрыто из ленты.", show_alert=False)
    
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


# ---------------- РАЗДЕЛ: ГДЕ Я БЫЛ ----------------

@dp.message(F.text.in_(["✅ Где я был", "Где я был", "Посещенные места"]))
async def show_visited_places(message: types.Message):
    user_id = message.from_user.id
    visited = db.get_user_visited(user_id)
    
    if not visited:
        await message.answer(
            "✅ <b>Список посещенных мест пока пуст!</b>\n\n"
            "Когда листаешь споты (<b>«🧭 Смотреть локации»</b>), "
            "нажимай <b>«✅ Уже был здесь (больше не показывать)»</b> на знакомых точках. "
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
    place_id = callback.data.split("_", 2)[2]
    place = db.get_place_by_id(place_id)
    
    if not place:
        await callback.answer("Локация не найдена!", show_alert=True)
        return
        
    card_text = format_card(place)
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🗺 Маршрут в Яндекс.Картах", url=place["yandex_map"]))
    builder.row(InlineKeyboardButton(text="❌ Вернуть место в ленту рекомендаций", callback_data=f"vis_del_{place['id']}"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад к списку посещенных", callback_data="vis_back_list"))
    
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


# ---------------- ФИЛЬТР ПО ВРЕМЕНИ / НОЧНЫЕ МЕСТА ----------------

@dp.message(F.text.in_(["🌙 Ночной фильтр (24/7)", "🌙 Ночной фильтр", "Ночной фильтр", "Ночные места"]))
async def select_time_filter(message: types.Message):
    user_id = message.from_user.id
    current_filter = db.get_user_time_filter(user_id)
    
    builder = InlineKeyboardBuilder()
    for key, label in TIME_FILTERS.items():
        mark = "✅ " if key == current_filter else ""
        builder.row(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"set_time_{key}"))
        
    await message.answer(
        "🌙 <b>Выбор фильтра по времени суток и доступности:</b>\n\n"
        "• <b>🌙 Ночные споты 24/7</b> (по умолчанию): только места со свободным круглосуточным въездом "
        "(без заборов, шлагбаумов и закрытых ворот) — где можно встать на машине, постоять, покурить и отдохнуть.\n"
        "• <b>🌅 Закат и вечер</b>: видовые точки у воды и на возвышенностях.\n"
        "• <b>☀️ Дневные</b>: парки, экотропы и прогулки.\n"
        "• <b>🌐 Любое время</b>: все доступные локации.",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


@dp.callback_query(F.data.startswith("set_time_"))
async def save_time_filter(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    key = callback.data.split("_", 2)[2]
    
    db.set_user_time_filter(user_id, key)
    time_name = TIME_FILTERS.get(key, "Любое время")
    
    await callback.answer(f"Режим обновлен: {time_name}", show_alert=False)
    await callback.message.edit_text(
        f"✅ <b>Установлен фильтр времени:</b> {time_name}\n\n"
        "Теперь нажми <b>«🧭 Смотреть локации»</b>, чтобы продолжить поиск!",
        parse_mode="HTML"
    )


# ---------------- ФИЛЬТР РАССТОЯНИЯ (С ШАГОМ 20, 30, 40, 50, ДО 60 КМ) ----------------

@dp.message(F.text.in_(["📏 Дистанция (до 60 км)", "📏 Дистанция (до 50 км)", "📏 Фильтр расстояния", "Фильтр расстояния", "📏 Дистанция", "Дистанция"]))
async def change_distance_filter(message: types.Message):
    user_id = message.from_user.id
    current_filter = db.get_user_distance_filter(user_id)
    
    builder = InlineKeyboardBuilder()
    for key, label in DISTANCE_GROUPS.items():
        mark = "✅ " if key == current_filter else ""
        builder.row(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"set_dist_{key}"))
        
    await message.answer(
        "📏 <b>Выбери максимальный радиус поездки от м. «Озерки»:</b>\n\n"
        "Выбирай комфортную для себя дальность:\n"
        "• <b>До 20 км</b> — быстрый выезд на 15–25 минут (Шувалово, Озерки, Юкки, Лисий Нос, Сертолово)\n"
        "• <b>До 30 км</b> — ближний Курортный и озера (Сестрорецк, Разлив, Медное озеро, Токсово)\n"
        "• <b>До 40 км</b> — побережье и Дамба (Репино, Комарово, Северная Дамба, Лемболово)\n"
        "• <b>До 50 км</b> — дальний берег и Ладога (Зеленогорск, Рощино, форт Шанц, Осиновец)\n"
        "• <b>До 60 км</b> — максимальный выезд (Приветнинское, Смолячково, Пески, Сосново)",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


@dp.callback_query(F.data.startswith("set_dist_"))
async def save_distance_filter(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    key = callback.data.split("_", 2)[2]
    
    db.set_user_distance_filter(user_id, key)
    dist_name = DISTANCE_GROUPS.get(key, f"До {key} км")
    
    await callback.answer(f"Дистанция: {dist_name}", show_alert=False)
    await callback.message.edit_text(
        f"✅ <b>Установлен радиус:</b> {dist_name}\n\n"
        "Локации дальше этой дистанции показываться не будут. "
        "Жми <b>«🧭 Смотреть локации»</b>!",
        parse_mode="HTML"
    )


# ---------------- ВЫБОР ЗОНЫ ----------------

@dp.message(F.text.in_(["📍 Выбор зоны", "Выбор зоны", "Зона"]))
async def select_zone_filter(message: types.Message):
    user_id = message.from_user.id
    current_zone = db.get_user_zone_filter(user_id)
    
    builder = InlineKeyboardBuilder()
    for key, label in ZONE_NAMES.items():
        mark = "✅ " if key == current_zone else ""
        builder.row(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"set_zone_{key}"))
        
    await message.answer(
        "📍 <b>Выбери район для поиска (северная часть СПб и области):</b>\n\n"
        "• <b>Курортный р-н</b>: берег Финского залива от Сестрорецка до Зеленогорска и Приветнинского.\n"
        "• <b>Карельский перешеек & Озера</b>: Токсово, Рощино, Медное озеро, Лемболово, холмы.\n"
        "• <b>Север СПб</b>: Озерки, Шувалово, Юкки, Лахта-Центр, Крестовский.\n"
        "• <b>Дамба и Кронштадт</b>: створные молы посреди моря, форт Шанц.\n"
        "• <b>Ладога</b>: песчаные пляжи Коккорево и Осиновецкий маяк.",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


@dp.callback_query(F.data.startswith("set_zone_"))
async def save_zone_filter(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    key = callback.data.split("_", 2)[2]
    
    db.set_user_zone_filter(user_id, key)
    zone_name = ZONE_NAMES.get(key, "Весь Север СПб и ЛО")
    
    await callback.answer(f"Зона: {zone_name}", show_alert=False)
    await callback.message.edit_text(
        f"✅ <b>Выбрана зона:</b> {zone_name}\n\n"
        "Жми <b>«🧭 Смотреть локации»</b>, чтобы исследовать споты в этом направлении!",
        parse_mode="HTML"
    )


# Поддержка старой кнопки обновления (если осталась в истории чата)
@dp.message(F.text.in_(["🔄 Обновить базу", "🔄 Обновить базу мест", "Обновить базу"]))
async def legacy_update_database(message: types.Message):
    added_count, total_count = db.auto_update_and_expand_places()
    await message.answer(
        f"✅ <b>База актуальна (всего {total_count} спотов до 60 км)!</b>\n\n"
        "Бот сам обновляет и дополняет базу каждые 6 часов в фоне, кнопку нажимать не требуется.",
        parse_mode="HTML",
        reply_markup=get_main_menu_keyboard()
    )


# ---------------- ИЗБРАННОЕ ----------------

@dp.message(F.text.in_(["⭐ Избранное", "Избранное"]))
async def show_favorites(message: types.Message):
    user_id = message.from_user.id
    favorites = db.get_user_favorites(user_id)
    
    if not favorites:
        await message.answer(
            "⭐ <b>Твое избранное пока пусто!</b>\n\n"
            "Нажми <b>«🧭 Смотреть локации»</b> и жми <b>❤️ В избранное</b> на понравившихся местах.",
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
    place_id = callback.data.split("_", 2)[2]
    place = db.get_place_by_id(place_id)
    
    if not place:
        await callback.answer("Локация не найдена!", show_alert=True)
        return
        
    card_text = format_card(place)
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🗺 Маршрут в Яндекс.Картах", url=place["yandex_map"]))
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


# ---------------- СТАТИСТИКА И ПРОФИЛЬ ----------------

@dp.message(F.text.in_(["📊 Мой профиль", "Мой профиль", "Профиль"]))
async def user_profile(message: types.Message):
    user_id = message.from_user.id
    stats = db.get_user_stats(user_id)
    
    fav_pref = stats["top_category"] if stats["top_category"] else "еще формируется"
    
    profile_text = (
        "📊 <b>Твой профиль ночного путешественника:</b>\n\n"
        f"👁 <b>Просмотрено спотов:</b> {stats['total_seen']} из {stats['total_available']}\n"
        f"❤️ <b>В избранном:</b> {stats['liked_count']}\n"
        f"✅ <b>Уже побывал:</b> {stats['visited_count']}\n"
        f"➡️ <b>Пропущено:</b> {stats['skipped_count']}\n\n"
        f"✨ <b>Любимый вайб:</b> {fav_pref}\n"
        f"🎯 <b>Радиус поиска:</b> {stats['dist_filter']}\n"
        f"🌙 <b>Режим времени:</b> {stats['time_filter']}\n"
        f"📍 <b>Направление:</b> {stats['zone_filter']}\n\n"
        "<i>База автоматически пополняется каждые 6 часов.</i>"
    )
    await message.answer(profile_text, parse_mode="HTML", reply_markup=get_main_menu_keyboard())


# ---------------- СБРОС ИСТОРИИ ----------------

@dp.message(F.text.in_(["🔁 Сбросить всё", "🔁 Сбросить историю", "Сбросить историю", "Сбросить всё"]))
async def reset_history_prompt(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="⚠️ Да, сбросить всё заново", callback_data="confirm_reset"),
        InlineKeyboardButton(text="Отмена", callback_data="cancel_reset")
    )
    await message.answer(
        "⚠️ <b>Ты точно хочешь сбросить всю историю?</b>\n\n"
        "Это очистит просмотренные места, избранное и отметки «Был здесь», "
        "чтобы ты мог пролистать базу с чистого листа.",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


@dp.callback_query(F.data == "confirm_reset")
async def confirm_reset_history(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    db.reset_user_history(user_id)
    
    await callback.answer("Вся история сброшена!", show_alert=True)
    await callback.message.edit_text(
        "🔄 <b>Вся история успешно очищена!</b>\n\n"
        "Все локации снова доступны. Жми <b>«🧭 Смотреть локации»</b>!",
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "cancel_reset")
async def cancel_reset_history(callback: types.CallbackQuery):
    await callback.message.delete()


# ---------------- ДОБАВЛЕНИЕ СВОЕГО МЕСТА ----------------

@dp.message(F.text.in_(["➕ Добавить место", "Добавить место"]))
async def start_add_place(message: types.Message, state: FSMContext):
    await state.set_state(AddPlaceForm.waiting_title)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Отмена", callback_data="cancel_add_place"))
    
    await message.answer(
        "📍 <b>Добавление новой секретной точки в каталог</b>\n\n"
        "Шаг 1 из 3: Введи <b>название места</b> (например: <i>Секретный мыс в Репино</i>):",
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
        "✨ Шаг 2 из 3: Опиши <b>вайб и почему круто ночью</b> (вид на воду, огни, можно ли постоять на машине, покурить):",
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
            "Она сразу стала доступна в общей ленте рекомендаций.",
            parse_mode="HTML",
            reply_markup=get_main_menu_keyboard()
        )
    else:
        await message.answer(
            "⚠️ Ошибка при сохранении. Попробуйте еще раз позже.",
            reply_markup=get_main_menu_keyboard()
        )


# ---------------- ФОНОВОЕ ОБНОВЛЕНИЕ БАЗЫ (КАЖДЫЕ 6 ЧАСОВ) ----------------

async def background_auto_updater():
    """Фоновое автоматическое обновление каждые 6 часов (21600 сек)."""
    while True:
        try:
            await asyncio.sleep(21600)  # 6 часов
            added, total = db.auto_update_and_expand_places()
            if added > 0:
                logger.info(f"[Авто-обновление] Успешно добавлено {added} новых локаций. Всего в базе: {total}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[Авто-обновление] Ошибка: {e}")
            await asyncio.sleep(3600)


async def start_health_check_server():
    """Веб-сервер для поддержания работы 24/7 на облачных хостингах."""
    app = web.Application()
    async def health_handler(request):
        return web.Response(text="SPB Ozerki Night Places Bot is running 24/7!")
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
    print("[+] Ночной бот атмосферных локаций севера СПб запущен!")
    print("[+] Настраиваемый радиус: 20 -> 30 -> 40 -> 50 -> 60 км.")
    print("[+] База расширена: 100+ локаций с фокусом на ночные споты 24/7.")
    print("[+] Кнопка обновления убрана из меню: работает авто-обновление каждые 6 часов.")
    print("=" * 60)
    
    # Запуск фонового health-check сервера
    await start_health_check_server()
    
    # Запуск фонового авто-обновления базы
    asyncio.create_task(background_auto_updater())
    
    # Запуск polling телеграма
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\n[+] Бот остановлен.")
