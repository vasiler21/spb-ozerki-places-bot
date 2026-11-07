# -*- coding: utf-8 -*-
import os
import sys
import logging
import asyncio
from typing import List, Dict, Any

# Исправление кодировки для вывода в консоль Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web

from places_data import PLACES, CATEGORY_NAMES, DISTANCE_GROUPS
import db

# Настройка логирования
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


def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Главная постоянная клавиатура бота."""
    kb = [
        [KeyboardButton(text="🧭 Смотреть локации"), KeyboardButton(text="⭐ Избранное")],
        [KeyboardButton(text="📏 Фильтр расстояния"), KeyboardButton(text="📊 Мой профиль")],
        [KeyboardButton(text="🔄 Сбросить историю")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


def format_card(place: Dict[str, Any]) -> str:
    """Форматирование одной карточки локации."""
    cat_emoji = CATEGORY_NAMES.get(place.get("category", ""), "📍")
    text = (
        f"<b>{place['title']}</b>\n"
        f"<i>{cat_emoji}</i>\n\n"
        f"⏱ <b>От м. Озерки:</b> ~{place['distance_km']} км ({place['car_time']})\n\n"
        f"✨ <b>Вайб (Pinterest/TikTok):</b>\n{place['vibe']}\n\n"
        f"📅 <b>Лучший сезон:</b> {place['season']}\n"
        f"🌅 <b>Время суток:</b> {place['time_of_day']}\n\n"
        f"🚙 <b>На машине (дорога и парковка):</b>\n{place['car_access']}\n\n"
        f"📸 <b>Идеи для фото:</b> {place['photo_tips']}\n"
    )
    return text


def get_swipe_keyboard(place_id: str, yandex_map_url: str) -> InlineKeyboardMarkup:
    """Инлайн-кнопки для листания карточек по одной (Like / Skip / Route)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="❤️ В избранное", callback_data=f"like_{place_id}"),
        InlineKeyboardButton(text="➡️ Пропустить", callback_data=f"skip_{place_id}")
    )
    builder.row(
        InlineKeyboardButton(text="🗺 Маршрут в Яндекс.Картах", url=yandex_map_url)
    )
    return builder.as_markup()


# ---------------- ХЕНДЛЕРЫ МЕНЮ ----------------

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    dist_filter = db.get_user_distance_filter(user_id)
    dist_name = DISTANCE_GROUPS.get(dist_filter, "Все")
    
    greeting = (
        "👋 <b>Добро пожаловать в гид по атмосферным локациям СПб и ЛО!</b>\n\n"
        "📍 <b>Точка старта: м. «Озерки»</b> (быстрый выезд на Выборгское ш., Скандинавию, Сортавалу и КАД).\n"
        "Здесь собраны самые вирусные и эстетичные места из <b>TikTok и Pinterest</b>: "
        "экотропы, маяки, песчаные дюны, карельские скалы и заброшенные кирхи.\n\n"
        "💡 <b>Как это работает:</b>\n"
        "• Локации показываются <b>по одной</b>.\n"
        "• Жми <b>❤️ В избранное</b>, чтобы сохранить в коллекцию.\n"
        "• Жми <b>➡️ Пропустить</b>, чтобы листать дальше (места не будут повторяться!).\n"
        "• Бот <b>запоминает твой вкус</b> и предлагает похожие места в первую очередь!\n\n"
        f"🎯 Текущий фильтр расстояния: <b>{dist_name}</b>"
    )
    await message.answer(greeting, parse_mode="HTML", reply_markup=get_main_menu_keyboard())


@dp.message(F.text == "🧭 Смотреть локации")
async def show_feed(message: types.Message):
    """Показать следующую подходящую локацию."""
    user_id = message.from_user.id
    next_place = db.get_next_smart_place(user_id)
    
    if not next_place:
        dist_filter = db.get_user_distance_filter(user_id)
        dist_name = DISTANCE_GROUPS.get(dist_filter, "выбранном радиусе")
        await message.answer(
            f"🎉 <b>Ты просмотрел все локации в фильтре «{dist_name}»!</b>\n\n"
            "Что можно сделать:\n"
            "1. Нажать <b>«📏 Фильтр расстояния»</b> и выбрать другой радиус (например, дальше 50 или 80 км).\n"
            "2. Открыть <b>«⭐ Избранное»</b> и изучить сохраненные места.\n"
            "3. Нажать <b>«🔄 Сбросить историю»</b>, чтобы пролистать все места заново.",
            parse_mode="HTML"
        )
        return
        
    card_text = format_card(next_place)
    kb = get_swipe_keyboard(next_place["id"], next_place["yandex_map"])
    await message.answer(card_text, parse_mode="HTML", reply_markup=kb)


# ---------------- РЕАКЦИИ: LIKE / SKIP ----------------

@dp.callback_query(F.data.startswith("like_"))
async def handle_like(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    place_id = callback.data.split("_", 1)[1]
    
    db.record_reaction(user_id, place_id, "liked")
    await callback.answer("Сохранено в Избранное! ⭐", show_alert=False)
    
    # Показываем следующее умное место
    next_place = db.get_next_smart_place(user_id)
    if not next_place:
        await callback.message.edit_text(
            "🎉 <b>Отлично! Ты просмотрел все доступные локации в этом фильтре!</b>\n\n"
            "Все сохраненные споты ждут тебя во вкладке <b>«⭐ Избранное»</b>.\n"
            "Чтобы расширить радиус, нажми <b>«📏 Фильтр расстояния»</b> в меню.",
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
            "Попробуй выбрать больший радиус в меню <b>«📏 Фильтр расстояния»</b> или сбросить историю в <b>«🔄 Сбросить историю»</b>.",
            parse_mode="HTML"
        )
        return
        
    card_text = format_card(next_place)
    kb = get_swipe_keyboard(next_place["id"], next_place["yandex_map"])
    await callback.message.edit_text(card_text, parse_mode="HTML", reply_markup=kb)


# ---------------- ИЗБРАННОЕ ----------------

@dp.message(F.text == "⭐ Избранное")
async def show_favorites(message: types.Message):
    user_id = message.from_user.id
    favorites = db.get_user_favorites(user_id)
    
    if not favorites:
        await message.answer(
            "⭐ <b>Твое избранное пока пусто!</b>\n\n"
            "Нажми <b>«🧭 Смотреть локации»</b> и жми <b>❤️ В избранное</b> на местах, которые тебе понравятся.",
            parse_mode="HTML"
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
    
    place = next((p for p in PLACES if p["id"] == place_id), None)
    if not place:
        await callback.answer("Локация не найдена!", show_alert=True)
        return
        
    card_text = format_card(place)
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🗺 Маршрут в Яндекс.Картах", url=place["yandex_map"]))
    builder.row(InlineKeyboardButton(text="❌ Удалить из избранного", callback_data=f"fav_del_{place['id']}"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад в Избранное", callback_data="fav_back_list"))
    
    await callback.message.edit_text(card_text, parse_mode="HTML", reply_markup=builder.as_markup())


@dp.callback_query(F.data.startswith("fav_del_"))
async def delete_favorite_place(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    place_id = callback.data.split("_", 2)[2]
    
    db.remove_favorite(user_id, place_id)
    await callback.answer("Удалено из избранного!", show_alert=True)
    
    # Обновляем список избранного
    favorites = db.get_user_favorites(user_id)
    if not favorites:
        await callback.message.edit_text(
            "⭐ <b>В избранном больше ничего нет.</b>\n"
            "Жми «🧭 Смотреть локации» в меню, чтобы найти новые!",
            parse_mode="HTML"
        )
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


# ---------------- ФИЛЬТР РАССТОЯНИЯ ----------------

@dp.message(F.text == "📏 Фильтр расстояния")
async def change_distance_filter(message: types.Message):
    user_id = message.from_user.id
    current_filter = db.get_user_distance_filter(user_id)
    
    builder = InlineKeyboardBuilder()
    for key, label in DISTANCE_GROUPS.items():
        mark = "✅ " if key == current_filter else ""
        builder.row(InlineKeyboardButton(text=f"{mark}{label}", callback_data=f"set_dist_{key}"))
        
    await message.answer(
        "📏 <b>Выбери максимальное расстояние от м. «Озерки»:</b>\n"
        "Бот будет предлагать локации в этом радиусе.",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


@dp.callback_query(F.data.startswith("set_dist_"))
async def save_distance_filter(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    key = callback.data.split("_", 2)[2]
    
    db.set_user_distance_filter(user_id, key)
    dist_name = DISTANCE_GROUPS.get(key, "Все")
    
    await callback.answer(f"Фильтр обновлен: {dist_name}", show_alert=False)
    await callback.message.edit_text(
        f"✅ <b>Установлен фильтр:</b> {dist_name}\n\n"
        "Теперь нажми <b>«🧭 Смотреть локации»</b>, чтобы продолжить просмотр с учетом нового расстояния!",
        parse_mode="HTML"
    )


# ---------------- СТАТИСТИКА И ПРОФИЛЬ ----------------

@dp.message(F.text == "📊 Мой профиль")
async def user_profile(message: types.Message):
    user_id = message.from_user.id
    stats = db.get_user_stats(user_id)
    dist_filter = db.get_user_distance_filter(user_id)
    dist_name = DISTANCE_GROUPS.get(dist_filter, "Все")
    
    fav_pref = stats["top_category"] if stats["top_category"] else "еще формируется (лайкни пару мест)"
    
    profile_text = (
        "📊 <b>Твой профиль путешественника:</b>\n\n"
        f"👁 <b>Просмотрено мест:</b> {stats['total_seen']} из {stats['total_available']}\n"
        f"❤️ <b>В избранном:</b> {stats['liked_count']}\n"
        f"➡️ <b>Пропущено:</b> {stats['skipped_count']}\n\n"
        f"✨ <b>Твой любимый вайб:</b> {fav_pref}\n"
        f"🎯 <b>Радиус поиска:</b> {dist_name}\n\n"
        "<i>На основе твоих лайков алгоритм ставит похожие места вперед ленты.</i>"
    )
    await message.answer(profile_text, parse_mode="HTML", reply_markup=get_main_menu_keyboard())


# ---------------- СБРОС ИСТОРИИ ----------------

@dp.message(F.text == "🔄 Сбросить историю")
async def reset_history_prompt(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="⚠️ Да, сбросить всё заново", callback_data="confirm_reset"),
        InlineKeyboardButton(text="Отмена", callback_data="cancel_reset")
    )
    await message.answer(
        "⚠️ <b>Ты точно хочешь сбросить всю историю?</b>\n\n"
        "Это очистит список просмотренных мест и избранное, "
        "чтобы ты мог пролистать все локации с чистого листа.",
        parse_mode="HTML",
        reply_markup=builder.as_markup()
    )


@dp.callback_query(F.data == "confirm_reset")
async def confirm_reset_history(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    db.reset_user_history(user_id)
    
    await callback.answer("История полностью очищена!", show_alert=True)
    await callback.message.edit_text(
        "🔄 <b>Вся статистика и история успешно сброшены!</b>\n\n"
        "Теперь все локации снова доступны к просмотру. Нажимай <b>«🧭 Смотреть локации»</b>!",
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "cancel_reset")
async def cancel_reset_history(callback: types.CallbackQuery):
    await callback.message.delete()


async def start_health_check_server():
    """Мини веб-сервер для облачных платформ (Render, Koyeb и т.д.), чтобы сервис не засыпал."""
    app = web.Application()
    async def health_handler(request):
        return web.Response(text="SPB Ozerki Bot is running 24/7!")
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    
    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    try:
        await site.start()
        logger.info(f"Health-check веб-сервер запущен на порту {port}")
    except Exception as e:
        logger.warning(f"Не удалось запустить веб-сервер на порту {port}: {e}")


# ---------------- ТОЧКА ВХОДА ----------------

async def main():
    if not bot:
        print("[!] Ошибка: Не указан токен бота в .env или BOT_TOKEN!")
        sys.exit(1)
        
    print("=" * 60)
    print("[+] Умный бот локаций СПб (старт: м. Озерки) запущен!")
    print("[+] База данных SQLite подключена.")
    print("=" * 60)
    
    # Запускаем фоновый health-check сервер для облака
    await start_health_check_server()
    
    # Запуск polling телеграма
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\n[+] Бот остановлен.")
