# -*- coding: utf-8 -*-
"""
Управление базой данных SQLite для бота атмосферных локаций СПб и ЛО.
Включает:
- Управление местами, миграцию схемы и синхронизацию.
- Реакции пользователя: 'liked' (избранное), 'skipped' (пропуск), 'visited' (я здесь был).
- Фильтры пользователя: дистанция (строго до 50 км), время суток (по умолчанию: 24/7 ночные), зона (север СПб и ЛО).
- Умный алгоритм подбора мест с приоритизацией секретных спотов и ночного чилла.
"""

import sqlite3
import os
import random
import logging
from typing import List, Dict, Optional, Tuple, Any
from places_data import PLACES, CATEGORY_NAMES, DISTANCE_GROUPS, ZONE_NAMES, TIME_FILTERS

logger = logging.getLogger(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), "bot_data.db")


def get_connection():
    return sqlite3.connect(DB_PATH)


def init_db():
    """Инициализация таблиц базы данных и автоматическая миграция новых полей."""
    with get_connection() as conn:
        cursor = conn.cursor()
        
        # 1. Таблица локаций
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS places (
                id TEXT PRIMARY KEY,
                title TEXT,
                category TEXT,
                zone TEXT DEFAULT 'north_spb',
                distance_km INTEGER,
                car_time TEXT,
                range_group TEXT,
                is_24_7 INTEGER DEFAULT 1,
                opening_hours TEXT DEFAULT 'Круглосуточно 24/7',
                night_vibe TEXT DEFAULT '',
                car_chill INTEGER DEFAULT 1,
                popularity TEXT DEFAULT 'hidden',
                vibe TEXT,
                season TEXT,
                time_of_day TEXT,
                car_access TEXT,
                photo_tips TEXT,
                yandex_map TEXT,
                is_custom INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Миграция колонок для places
        cursor.execute("PRAGMA table_info(places)")
        existing_cols = {col[1] for col in cursor.fetchall()}
        
        columns_to_add = [
            ("zone", "TEXT DEFAULT 'north_spb'"),
            ("is_24_7", "INTEGER DEFAULT 1"),
            ("opening_hours", "TEXT DEFAULT 'Круглосуточно 24/7'"),
            ("night_vibe", "TEXT DEFAULT ''"),
            ("car_chill", "INTEGER DEFAULT 1"),
            ("popularity", "TEXT DEFAULT 'hidden'")
        ]
        for col_name, col_def in columns_to_add:
            if col_name not in existing_cols:
                cursor.execute(f"ALTER TABLE places ADD COLUMN {col_name} {col_def}")
        
        # 2. Таблица реакций (лайк / пропуск / я здесь был)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_reactions (
                user_id INTEGER,
                place_id TEXT,
                action TEXT, -- 'liked', 'skipped', 'visited'
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, place_id)
            )
        """)
        
        # 3. Таблица настроек пользователя (ночной фильтр по умолчанию включен!)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                distance_filter TEXT DEFAULT '50',
                time_filter TEXT DEFAULT 'night',
                zone_filter TEXT DEFAULT 'all_north',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Миграция колонок для user_settings
        cursor.execute("PRAGMA table_info(user_settings)")
        existing_settings_cols = {col[1] for col in cursor.fetchall()}
        
        settings_to_add = [
            ("time_filter", "TEXT DEFAULT 'night'"),
            ("zone_filter", "TEXT DEFAULT 'all_north'")
        ]
        for col_name, col_def in settings_to_add:
            if col_name not in existing_settings_cols:
                cursor.execute(f"ALTER TABLE user_settings ADD COLUMN {col_name} {col_def}")
                
        # 4. Удаляем старые нерелевантные локации с расстоянием > 50 км
        cursor.execute("DELETE FROM places WHERE distance_km > 50")
        
        conn.commit()

    # Синхронизируем базовые места из каталога и пула открытий
    sync_places_from_catalog()


def sync_places_from_catalog() -> int:
    """
    Синхронизирует локации из places_data.py и пула открытий в таблицу places.
    """
    added_count = 0
    with get_connection() as conn:
        cursor = conn.cursor()
        
        # 1. Загрузка из PLACES
        for p in PLACES:
            cursor.execute("""
                INSERT INTO places (
                    id, title, category, zone, distance_km, car_time, range_group,
                    is_24_7, opening_hours, night_vibe, car_chill, popularity,
                    vibe, season, time_of_day, car_access, photo_tips, yandex_map, is_custom
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    category = excluded.category,
                    zone = excluded.zone,
                    distance_km = excluded.distance_km,
                    car_time = excluded.car_time,
                    range_group = excluded.range_group,
                    is_24_7 = excluded.is_24_7,
                    opening_hours = excluded.opening_hours,
                    night_vibe = excluded.night_vibe,
                    car_chill = excluded.car_chill,
                    popularity = excluded.popularity,
                    vibe = excluded.vibe,
                    season = excluded.season,
                    time_of_day = excluded.time_of_day,
                    car_access = excluded.car_access,
                    photo_tips = excluded.photo_tips,
                    yandex_map = excluded.yandex_map
            """, (
                p["id"], p["title"], p["category"], p.get("zone", "north_spb"),
                p["distance_km"], p["car_time"], p.get("range_group", "50"),
                p.get("is_24_7", 1), p.get("opening_hours", "Круглосуточно 24/7"),
                p.get("night_vibe", ""), p.get("car_chill", 1), p.get("popularity", "hidden"),
                p["vibe"], p["season"], p["time_of_day"], p["car_access"],
                p["photo_tips"], p["yandex_map"]
            ))
            added_count += 1
            
        # Удаляем любые точки дальше 50 км
        cursor.execute("DELETE FROM places WHERE distance_km > 50")
        conn.commit()
        
    return added_count


def get_all_places_from_db() -> List[Dict[str, Any]]:
    """Получить все активные локации из базы данных (строго <= 50 км)."""
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, category, zone, distance_km, car_time, range_group,
                   is_24_7, opening_hours, night_vibe, car_chill, popularity,
                   vibe, season, time_of_day, car_access, photo_tips, yandex_map, is_custom
            FROM places
            WHERE distance_km <= 50
            ORDER BY distance_km ASC
        """)
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


def get_place_by_id(place_id: str) -> Optional[Dict[str, Any]]:
    """Получить одну локацию по её ID."""
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, category, zone, distance_km, car_time, range_group,
                   is_24_7, opening_hours, night_vibe, car_chill, popularity,
                   vibe, season, time_of_day, car_access, photo_tips, yandex_map, is_custom
            FROM places
            WHERE id = ?
        """, (place_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def add_custom_place(place_dict: Dict[str, Any]) -> bool:
    """Добавить новую пользовательскую локацию в базу данных."""
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO places (
                    id, title, category, zone, distance_km, car_time, range_group,
                    is_24_7, opening_hours, night_vibe, car_chill, popularity,
                    vibe, season, time_of_day, car_access, photo_tips, yandex_map, is_custom
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    vibe = excluded.vibe,
                    yandex_map = excluded.yandex_map
            """, (
                place_dict["id"], place_dict["title"], place_dict.get("category", "night_chill"),
                place_dict.get("zone", "north_spb"),
                min(place_dict.get("distance_km", 25), 50),
                place_dict.get("car_time", "25-30 мин"),
                place_dict.get("range_group", "25"),
                place_dict.get("is_24_7", 1),
                place_dict.get("opening_hours", "Круглосуточно 24/7"),
                place_dict.get("night_vibe", "Отличное место для ночного чилла"),
                place_dict.get("car_chill", 1),
                place_dict.get("popularity", "secret"),
                place_dict["vibe"],
                place_dict.get("season", "Круглый год"),
                place_dict.get("time_of_day", "Круглосуточно"),
                place_dict.get("car_access", "Подъезд на авто доступен"),
                place_dict.get("photo_tips", "Видовые кадры природы и воды"),
                place_dict.get("yandex_map", "https://yandex.ru/maps/")
            ))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Ошибка при добавлении места: {e}")
            return False


def auto_update_and_expand_places() -> Tuple[int, int]:
    """Делегирует обновление модулю places_updater."""
    import places_updater
    return places_updater.update_and_expand_places()


# ---------------- НАСТРОЙКИ ПОЛЬЗОВАТЕЛЯ ----------------

def set_user_distance_filter(user_id: int, distance_filter: str):
    """Установить фильтр максимального расстояния (15, 25, 35, 50)."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_settings (user_id, distance_filter)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                distance_filter = excluded.distance_filter,
                updated_at = CURRENT_TIMESTAMP
        """, (user_id, distance_filter))
        conn.commit()


def get_user_distance_filter(user_id: int) -> str:
    """Получить текущий фильтр расстояния (по умолчанию '50' км)."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT distance_filter FROM user_settings WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        val = row[0] if row and row[0] else "50"
        if val in ("all", "80", "100+"):
            val = "50"
        return val


def set_user_time_filter(user_id: int, time_filter: str):
    """Установить фильтр по времени суток: 'night', 'sunset', 'day', 'all'."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_settings (user_id, time_filter)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                time_filter = excluded.time_filter,
                updated_at = CURRENT_TIMESTAMP
        """, (user_id, time_filter))
        conn.commit()


def get_user_time_filter(user_id: int) -> str:
    """Получить текущий фильтр времени суток (по умолчанию: 'night' для ночных спотов 24/7)."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT time_filter FROM user_settings WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row[0] if row and row[0] else "night"


def set_user_zone_filter(user_id: int, zone_filter: str):
    """Установить фильтр зоны: 'all_north', 'kurortny', 'priozersk_vyborg', 'north_spb', 'damba_kronstadt', 'vsevolozhsk_ladoga'."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_settings (user_id, zone_filter)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                zone_filter = excluded.zone_filter,
                updated_at = CURRENT_TIMESTAMP
        """, (user_id, zone_filter))
        conn.commit()


def get_user_zone_filter(user_id: int) -> str:
    """Получить текущий фильтр зоны (по умолчанию 'all_north')."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT zone_filter FROM user_settings WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row[0] if row and row[0] else "all_north"


# ---------------- РЕАКЦИИ ПОЛЬЗОВАТЕЛЯ (LIKE / SKIP / VISITED) ----------------

def record_reaction(user_id: int, place_id: str, action: str):
    """
    Записать реакцию:
    - 'liked' (в избранное)
    - 'skipped' (пропуск)
    - 'visited' (я здесь уже был)
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_reactions (user_id, place_id, action)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, place_id) DO UPDATE SET
                action = excluded.action,
                created_at = CURRENT_TIMESTAMP
        """, (user_id, place_id, action))
        conn.commit()


def remove_favorite(user_id: int, place_id: str):
    """Удалить локацию из избранного."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE user_reactions
            SET action = 'skipped'
            WHERE user_id = ? AND place_id = ?
        """, (user_id, place_id))
        conn.commit()


def remove_visited(user_id: int, place_id: str):
    """Снять отметку «Был здесь»."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM user_reactions
            WHERE user_id = ? AND place_id = ? AND action = 'visited'
        """, (user_id, place_id))
        conn.commit()


def get_user_favorites(user_id: int) -> List[Dict[str, Any]]:
    """Получить список всех избранных локаций пользователя."""
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.* FROM user_reactions r
            JOIN places p ON r.place_id = p.id
            WHERE r.user_id = ? AND r.action = 'liked'
            ORDER BY r.created_at DESC
        """, (user_id,))
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


def get_user_visited(user_id: int) -> List[Dict[str, Any]]:
    """Получить список локаций, где пользователь уже был."""
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.* FROM user_reactions r
            JOIN places p ON r.place_id = p.id
            WHERE r.user_id = ? AND r.action = 'visited'
            ORDER BY r.created_at DESC
        """, (user_id,))
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


def reset_user_history(user_id: int):
    """Полный сброс истории: очистка лайков, пропусков, отметок 'был здесь' и фильтров."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM user_reactions WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))
        conn.commit()


def get_user_stats(user_id: int) -> Dict[str, Any]:
    """Получить расширенную статистику пользователя."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT action, COUNT(*) FROM user_reactions
            WHERE user_id = ?
            GROUP BY action
        """, (user_id,))
        counts = dict(cursor.fetchall())
        
        cursor.execute("""
            SELECT p.category FROM user_reactions r
            JOIN places p ON r.place_id = p.id
            WHERE r.user_id = ? AND r.action = 'liked'
        """, (user_id,))
        categories = [r[0] for r in cursor.fetchall()]

        cursor.execute("SELECT COUNT(*) FROM places WHERE distance_km <= 50")
        total_available = cursor.fetchone()[0]
    
    liked_count = counts.get("liked", 0)
    skipped_count = counts.get("skipped", 0)
    visited_count = counts.get("visited", 0)
    total_seen = liked_count + skipped_count + visited_count
    
    cat_likes: Dict[str, int] = {}
    for cat in categories:
        cat_likes[cat] = cat_likes.get(cat, 0) + 1
            
    top_cat = None
    if cat_likes:
        top_cat_key = max(cat_likes, key=cat_likes.get)
        top_cat = CATEGORY_NAMES.get(top_cat_key, top_cat_key)
        
    dist_filter = get_user_distance_filter(user_id)
    time_filter = get_user_time_filter(user_id)
    zone_filter = get_user_zone_filter(user_id)
        
    return {
        "total_seen": total_seen,
        "liked_count": liked_count,
        "skipped_count": skipped_count,
        "visited_count": visited_count,
        "total_available": total_available,
        "top_category": top_cat,
        "dist_filter": DISTANCE_GROUPS.get(dist_filter, f"До {dist_filter} км"),
        "time_filter": TIME_FILTERS.get(time_filter, "Ночные споты 24/7"),
        "zone_filter": ZONE_NAMES.get(zone_filter, "Весь Север СПб и ЛО")
    }


# ---------------- УМНЫЙ АЛГОРИТМ ПОДБОРА ЛОКАЦИЙ ----------------

def get_next_smart_place(user_id: int) -> Optional[Dict[str, Any]]:
    """
    Интеллектуальный подбор следующей локации:
    1. Исключает просмотренные (лайкнутые, пропущенные И отмеченные как 'visited' - «Был здесь»).
    2. Строго соблюдает числовой фильтр расстояния: distance_km <= max_km (строго <= 50 км!).
    3. При фильтре 'night' (по умолчанию) выбирает ТОЛЬКО круглосуточные места (is_24_7 == 1) с открытым въездом!
    4. Учитывает выбранную географическую зону севера СПб и области.
    5. Ранжирует в пользу секретных точек (popularity: 'secret', 'hidden') и комфортного авто-чилла (car_chill == 1).
    """
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Получаем все реакции (лайк, скип, был здесь)
        cursor.execute("SELECT place_id, action FROM user_reactions WHERE user_id = ?", (user_id,))
        reactions = cursor.fetchall()
        seen_ids = {r["place_id"] for r in reactions}
        
        # Лайки по категориям
        cursor.execute("""
            SELECT p.category, COUNT(*) as cnt FROM user_reactions r
            JOIN places p ON r.place_id = p.id
            WHERE r.user_id = ? AND r.action = 'liked'
            GROUP BY p.category
        """, (user_id,))
        cat_likes = {r["category"]: r["cnt"] for r in cursor.fetchall()}

    all_places = get_all_places_from_db()
    dist_filter_str = get_user_distance_filter(user_id)
    time_filter = get_user_time_filter(user_id)
    zone_filter = get_user_zone_filter(user_id)
    
    try:
        max_dist_km = min(int(dist_filter_str), 50)
    except Exception:
        max_dist_km = 50

    # 1. Отсеиваем уже увиденные и посещенные места
    candidates = [p for p in all_places if p["id"] not in seen_ids]
    
    # 2. Строгий фильтр по дальности (никаких мест дальше выбранного радиуса, и не больше 50 км!)
    candidates = [p for p in candidates if p["distance_km"] <= max_dist_km]
    
    # 3. Фильтр по времени суток
    if time_filter == "night":
        # ТОЛЬКО места с круглосуточным доступом 24/7 и открытым въездом!
        candidates = [p for p in candidates if p.get("is_24_7", 1) == 1]
    elif time_filter == "sunset":
        candidates = [p for p in candidates if "закат" in p.get("time_of_day", "").lower() or p.get("category") in ("coast", "viewpoint")]
    elif time_filter == "day":
        candidates = [p for p in candidates if "день" in p.get("time_of_day", "").lower() or p.get("category") in ("nature", "architecture")]

    # 4. Фильтр по зоне
    if zone_filter != "all_north":
        candidates = [p for p in candidates if p.get("zone") == zone_filter]
        
    if not candidates:
        return None
        
    # 5. Умное ранжирование (приоритет авто-чиллу и секретным точкам)
    def score_place(place: Dict[str, Any]) -> float:
        score = 10.0
        
        # Огромный приоритет секретным и небанальным спотам
        pop = place.get("popularity", "hidden")
        if pop == "secret":
            score += 25.0
        elif pop == "hidden":
            score += 15.0
            
        # Огромный приоритет авто-чиллу (постоять в машине, вид, покурить)
        if place.get("car_chill", 0) == 1:
            score += 20.0
            
        # Категории ночного чилла и смотровых
        if place.get("category") == "night_chill":
            score += 18.0
        elif place.get("category") == "viewpoint":
            score += 12.0
            
        # Бонус за любимые категории пользователя
        score += cat_likes.get(place.get("category", ""), 0) * 3.0
        
        # Небольшая вариативность
        score *= random.uniform(0.9, 1.15)
        return score

    candidates.sort(key=score_place, reverse=True)
    return candidates[0]
