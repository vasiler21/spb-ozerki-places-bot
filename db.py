# -*- coding: utf-8 -*-
"""
Управление базой данных SQLite для бота атмосферных локаций СПб и ЛО.
Включает:
- Управление местами, миграцию схемы и синхронизацию.
- Реакции пользователя: 'liked' (избранное), 'skipped' (пропуск), 'visited' (я здесь был).
- Динамический выбор точки старта: любая станция метро СПб (м. Озерки, м. Гражданский пр., и др.) или GPS-геолокация.
- Динамический расчет точного расстояния, времени в пути на авто и маршрутов Яндекс.Карт от выбранного метро.
- Умный подбор под атмосферу времени суток (Ночь, Закат/вечер, День, Утро/рассвет) с проверкой доступности.
- Мгновенное кеширование каталога в памяти (< 1 мс).
"""

import sqlite3
import os
import re
import random
import logging
from typing import List, Dict, Optional, Tuple, Any

from places_data import (
    PLACES, CATEGORY_NAMES, DISTANCE_GROUPS, ZONE_NAMES, TIME_FILTERS,
    METRO_STATIONS, DEFAULT_METRO_ID, haversine_distance_km, estimate_car_time
)
from places_updater import AUTO_DISCOVERY_POOL

logger = logging.getLogger(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), "bot_data.db")

# Кеш мест в оперативной памяти для моментального отклика
_PLACES_CACHE: List[Dict[str, Any]] = []


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
                lat REAL DEFAULT 60.0371,
                lon REAL DEFAULT 30.3162,
                best_times TEXT DEFAULT 'night,sunset,day,morning',
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
            ("popularity", "TEXT DEFAULT 'hidden'"),
            ("lat", "REAL DEFAULT 60.0371"),
            ("lon", "REAL DEFAULT 30.3162"),
            ("best_times", "TEXT DEFAULT 'night,sunset,day,morning'")
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
        
        # 3. Таблица настроек пользователя
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                distance_filter TEXT DEFAULT '60',
                time_filter TEXT DEFAULT 'night',
                zone_filter TEXT DEFAULT 'all_north',
                start_point TEXT DEFAULT 'ozerki',
                custom_lat REAL,
                custom_lon REAL,
                custom_name TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Миграция колонок для user_settings
        cursor.execute("PRAGMA table_info(user_settings)")
        existing_settings_cols = {col[1] for col in cursor.fetchall()}
        
        settings_to_add = [
            ("time_filter", "TEXT DEFAULT 'night'"),
            ("zone_filter", "TEXT DEFAULT 'all_north'"),
            ("start_point", "TEXT DEFAULT 'ozerki'"),
            ("custom_lat", "REAL"),
            ("custom_lon", "REAL"),
            ("custom_name", "TEXT")
        ]
        for col_name, col_def in settings_to_add:
            if col_name not in existing_settings_cols:
                cursor.execute(f"ALTER TABLE user_settings ADD COLUMN {col_name} {col_def}")
                
        # 4. Таблица системных метаданных (время авто-обновления и т.д.)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bot_metadata (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()

    # Синхронизируем базовые места из каталога и пула открытий
    sync_places_from_catalog()


def get_metadata(key: str, default: Optional[str] = None) -> Optional[str]:
    """Получить значение системного метаданного."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM bot_metadata WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row[0] if row and row[0] is not None else default
    except Exception:
        return default


def set_metadata(key: str, value: str):
    """Установить значение системного метаданного."""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO bot_metadata (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
            """, (key, value))
            conn.commit()
    except Exception as e:
        logger.error(f"Ошибка сохранения метаданных: {e}")


def sync_places_from_catalog() -> int:
    """
    Синхронизирует локации из places_data.py и пула открытий в таблицу places.
    Парсит координаты и вычисляет пригодность по времени суток.
    """
    global _PLACES_CACHE
    added_count = 0
    with get_connection() as conn:
        cursor = conn.cursor()
        
        from places_updater import load_discovered_places_from_file
        discovered = load_discovered_places_from_file()
        
        seen_keys = set()
        all_to_sync = []
        for p in list(PLACES) + list(AUTO_DISCOVERY_POOL) + list(discovered):
            if p["id"] not in seen_keys:
                seen_keys.add(p["id"])
                all_to_sync.append(p)
        
        for p in all_to_sync:
            # Извлекаем lat и lon
            lat = p.get("lat")
            lon = p.get("lon")
            if lat is None or lon is None:
                m = re.search(r"pt=([0-9.]+),([0-9.]+)", p.get("yandex_map", ""))
                if m:
                    lon = float(m.group(1))
                    lat = float(m.group(2))
                else:
                    lat, lon = 60.0371, 30.3162

            # Определяем best_times
            best = p.get("best_times")
            if isinstance(best, list):
                best_str = ",".join(best)
            elif isinstance(best, str):
                best_str = best
            else:
                cat = p.get("category", "")
                zone = p.get("zone", "")
                vibe = (p.get("vibe", "") + " " + p.get("night_vibe", "")).lower()
                is_24_7 = p.get("is_24_7", 1)
                b_list = []
                if is_24_7 and (p.get("car_chill") or p.get("night_vibe") or cat in ("night_chill", "viewpoint") or "ноч" in vibe or "огни" in vibe or "звезд" in vibe):
                    b_list.append("night")
                if cat in ("coast", "viewpoint", "night_chill") or "закат" in vibe or "залив" in vibe or zone in ("kurortny", "damba_kronstadt"):
                    b_list.append("sunset")
                if cat in ("nature", "architecture", "coast") or "экотроп" in vibe or "троп" in vibe or "лес" in vibe or "парк" in vibe or "пляж" in vibe:
                    b_list.append("day")
                if "рассвет" in vibe or "туман" in vibe or zone == "vsevolozhsk_ladoga" or cat == "nature" or "озер" in vibe:
                    b_list.append("morning")
                if not b_list:
                    b_list = ["night", "sunset", "day", "morning"]
                best_str = ",".join(b_list)

            cursor.execute("""
                INSERT INTO places (
                    id, title, category, zone, distance_km, car_time, range_group,
                    is_24_7, opening_hours, night_vibe, car_chill, popularity,
                    vibe, season, time_of_day, car_access, photo_tips, yandex_map,
                    lat, lon, best_times, is_custom
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    category = excluded.category,
                    zone = excluded.zone,
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
                    yandex_map = excluded.yandex_map,
                    lat = excluded.lat,
                    lon = excluded.lon,
                    best_times = excluded.best_times
            """, (
                p["id"], p["title"], p["category"], p.get("zone", "north_spb"),
                p.get("distance_km", 25), p.get("car_time", "25 мин"), p.get("range_group", "50"),
                p.get("is_24_7", 1), p.get("opening_hours", "Круглосуточно 24/7"),
                p.get("night_vibe", ""), p.get("car_chill", 1), p.get("popularity", "hidden"),
                p["vibe"], p.get("season", "Круглый год"), p.get("time_of_day", "Ночь / вечер / день"),
                p["car_access"], p["photo_tips"], p["yandex_map"],
                lat, lon, best_str
            ))
            added_count += 1
            
        conn.commit()

    # Обновляем кеш в памяти
    refresh_places_cache()
    return added_count


def refresh_places_cache():
    """Загружает все места из базы данных в оперативный кеш."""
    global _PLACES_CACHE
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, category, zone, distance_km, car_time, range_group,
                   is_24_7, opening_hours, night_vibe, car_chill, popularity,
                   vibe, season, time_of_day, car_access, photo_tips, yandex_map,
                   lat, lon, best_times, is_custom
            FROM places
        """)
        rows = cursor.fetchall()
        _PLACES_CACHE = [dict(r) for r in rows]
        for p in _PLACES_CACHE:
            if isinstance(p.get("best_times"), str):
                p["best_times_list"] = [t.strip() for t in p["best_times"].split(",") if t.strip()]
            else:
                p["best_times_list"] = ["night", "sunset", "day", "morning"]


def get_all_places_from_db() -> List[Dict[str, Any]]:
    """Получить все локации из оперативного кеша (мгновенно)."""
    global _PLACES_CACHE
    if not _PLACES_CACHE:
        refresh_places_cache()
    return list(_PLACES_CACHE)


def get_place_by_id(place_id: str) -> Optional[Dict[str, Any]]:
    """Получить одну локацию по её ID (из кеша или БД)."""
    for p in get_all_places_from_db():
        if p["id"] == place_id:
            return dict(p)
    return None


def add_custom_place(place_dict: Dict[str, Any]) -> bool:
    """Добавить новую пользовательскую локацию в базу данных."""
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            m = re.search(r"pt=([0-9.]+),([0-9.]+)", place_dict.get("yandex_map", ""))
            lon = float(m.group(1)) if m else 30.3162
            lat = float(m.group(2)) if m else 60.0371
            
            cursor.execute("""
                INSERT INTO places (
                    id, title, category, zone, distance_km, car_time, range_group,
                    is_24_7, opening_hours, night_vibe, car_chill, popularity,
                    vibe, season, time_of_day, car_access, photo_tips, yandex_map,
                    lat, lon, best_times, is_custom
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'night,sunset,day,morning', 1)
            """, (
                place_dict["id"], place_dict["title"], place_dict.get("category", "night_chill"),
                place_dict.get("zone", "north_spb"),
                place_dict.get("distance_km", 25),
                place_dict.get("car_time", "25-30 мин"),
                place_dict.get("range_group", "30"),
                place_dict.get("is_24_7", 1),
                place_dict.get("opening_hours", "Круглосуточно 24/7"),
                place_dict.get("night_vibe", "Отличное место для чилла"),
                place_dict.get("car_chill", 1),
                place_dict.get("popularity", "secret"),
                place_dict["vibe"],
                place_dict.get("season", "Круглый год"),
                place_dict.get("time_of_day", "Круглосуточно"),
                place_dict.get("car_access", "Подъезд на авто доступен"),
                place_dict.get("photo_tips", "Красивые панорамы"),
                place_dict.get("yandex_map", "https://yandex.ru/maps/"),
                lat, lon
            ))
            conn.commit()
            refresh_places_cache()
            return True
        except Exception as e:
            logger.error(f"Ошибка при добавлении места: {e}")
            return False


def auto_update_and_expand_places(force: bool = False) -> Tuple[int, int, List[str]]:
    """Делегирует 12-часовой поиск и анализ новых локаций модулю places_updater."""
    import places_updater
    return places_updater.update_and_expand_places(force=force)


def get_discovery_status() -> Dict[str, Any]:
    """Возвращает информацию о расписании авто-поиска локаций."""
    from datetime import datetime, timezone
    last_str = get_metadata("last_discovery_timestamp")
    last_cnt = get_metadata("last_discovery_count", "0")
    total = len(get_all_places_from_db())
    
    if last_str:
        try:
            ts = float(last_str)
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            formatted = dt.strftime("%d.%m %H:%M UTC")
            now_ts = datetime.now(timezone.utc).timestamp()
            elapsed_h = (now_ts - ts) / 3600.0
            next_in_h = max(0.0, 12.0 - elapsed_h)
        except Exception:
            formatted = "Недавно"
            next_in_h = 12.0
    else:
        formatted = "Еще не выполнялся"
        next_in_h = 0.0
        
    return {
        "last_run": formatted,
        "last_added": int(last_cnt or 0),
        "next_in_hours": round(next_in_h, 1),
        "total_places": total
    }


# ---------------- НАСТРОЙКИ ПОЛЬЗОВАТЕЛЯ ----------------

def get_user_settings(user_id: int) -> Dict[str, Any]:
    """Получить все настройки пользователя в одном быстром запросе."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT distance_filter, time_filter, zone_filter, start_point, custom_lat, custom_lon, custom_name
            FROM user_settings WHERE user_id = ?
        """, (user_id,))
        row = cursor.fetchone()
        if not row:
            return {
                "distance_filter": "60",
                "time_filter": "night",
                "zone_filter": "all_north",
                "start_point": DEFAULT_METRO_ID,
                "custom_lat": None,
                "custom_lon": None,
                "custom_name": None
            }
        dist = row[0] or "60"
        if dist in ("all", "80", "100+", "15"):
            dist = "60"
        return {
            "distance_filter": dist,
            "time_filter": row[1] or "night",
            "zone_filter": row[2] or "all_north",
            "start_point": row[3] or DEFAULT_METRO_ID,
            "custom_lat": row[4],
            "custom_lon": row[5],
            "custom_name": row[6]
        }


def get_user_start_point(user_id: int) -> Dict[str, Any]:
    """
    Возвращает информацию о текущей точке старта пользователя (метро или координаты).
    """
    settings = get_user_settings(user_id)
    sp = settings["start_point"]
    
    if sp == "custom" and settings["custom_lat"] is not None:
        return {
            "id": "custom",
            "name": settings["custom_name"] or "Моя геопозиция",
            "short_name": "Геопозиция",
            "line": "📍 GPS",
            "lat": settings["custom_lat"],
            "lon": settings["custom_lon"],
            "hint": "Текущая геопозиция"
        }
        
    metro = METRO_STATIONS.get(sp)
    if not metro:
        metro = METRO_STATIONS[DEFAULT_METRO_ID]
    return metro


def set_user_start_metro(user_id: int, metro_id: str):
    """Установить станцию метро как точку отсчета."""
    if metro_id not in METRO_STATIONS:
        metro_id = DEFAULT_METRO_ID
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_settings (user_id, start_point)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                start_point = excluded.start_point,
                updated_at = CURRENT_TIMESTAMP
        """, (user_id, metro_id))
        conn.commit()


def set_user_custom_location(user_id: int, lat: float, lon: float, name: str = "Моя геопозиция"):
    """Установить точные GPS-координаты пользователя как точку старта."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_settings (user_id, start_point, custom_lat, custom_lon, custom_name)
            VALUES (?, 'custom', ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                start_point = 'custom',
                custom_lat = excluded.custom_lat,
                custom_lon = excluded.custom_lon,
                custom_name = excluded.custom_name,
                updated_at = CURRENT_TIMESTAMP
        """, (user_id, lat, lon, name))
        conn.commit()


def set_user_distance_filter(user_id: int, distance_filter: str):
    """Установить фильтр максимального расстояния (20, 30, 40, 50, 60)."""
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
    """Получить текущий фильтр расстояния."""
    return get_user_settings(user_id)["distance_filter"]


def set_user_time_filter(user_id: int, time_filter: str):
    """Установить фильтр атмосферы/времени: 'night', 'sunset', 'day', 'morning', 'all'."""
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
    """Получить фильтр времени суток."""
    return get_user_settings(user_id)["time_filter"]


def set_user_zone_filter(user_id: int, zone_filter: str):
    """Установить фильтр географической зоны."""
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
    """Получить фильтр зоны."""
    return get_user_settings(user_id)["zone_filter"]


# ---------------- РЕАКЦИИ ПОЛЬЗОВАТЕЛЯ (LIKE / SKIP / VISITED) ----------------

def record_reaction(user_id: int, place_id: str, action: str):
    """
    Записать реакцию: 'liked' (избранное), 'skipped' (пропуск), 'visited' (был здесь).
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
    """Получить список всех избранных локаций пользователя с расстояниями от его точки."""
    start_pt = get_user_start_point(user_id)
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
        
    result = []
    for r in rows:
        d = dict(r)
        d_km = haversine_distance_km(start_pt["lat"], start_pt["lon"], d.get("lat", 60.0), d.get("lon", 30.0))
        d["distance_km"] = d_km
        d["car_time"] = estimate_car_time(d_km)
        d["start_name"] = start_pt["name"]
        d["yandex_map"] = f"https://yandex.ru/maps/?rtext={start_pt['lat']},{start_pt['lon']}~{d.get('lat', 60.0)},{d.get('lon', 30.0)}&rtt=auto"
        result.append(d)
    return result


def get_user_visited(user_id: int) -> List[Dict[str, Any]]:
    """Получить список локаций, где пользователь уже был."""
    start_pt = get_user_start_point(user_id)
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
        
    result = []
    for r in rows:
        d = dict(r)
        d_km = haversine_distance_km(start_pt["lat"], start_pt["lon"], d.get("lat", 60.0), d.get("lon", 30.0))
        d["distance_km"] = d_km
        d["car_time"] = estimate_car_time(d_km)
        d["start_name"] = start_pt["name"]
        d["yandex_map"] = f"https://yandex.ru/maps/?rtext={start_pt['lat']},{start_pt['lon']}~{d.get('lat', 60.0)},{d.get('lon', 30.0)}&rtt=auto"
        result.append(d)
    return result


def reset_user_history(user_id: int):
    """Полный сброс истории реакций пользователя (оставляет настройки точки старта)."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM user_reactions WHERE user_id = ?", (user_id,))
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

    all_places = get_all_places_from_db()
    total_available = len(all_places)
    
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
        
    settings = get_user_settings(user_id)
    start_pt = get_user_start_point(user_id)
    
    return {
        "total_seen": total_seen,
        "liked_count": liked_count,
        "skipped_count": skipped_count,
        "visited_count": visited_count,
        "total_available": total_available,
        "top_category": top_cat,
        "start_name": start_pt["name"],
        "dist_filter": DISTANCE_GROUPS.get(settings["distance_filter"], f"До {settings['distance_filter']} км"),
        "time_filter": TIME_FILTERS.get(settings["time_filter"], "Ночь — огни, авто-чилл"),
        "zone_filter": ZONE_NAMES.get(settings["zone_filter"], "Весь Север СПб и ЛО")
    }


# ---------------- УМНЫЙ АЛГОРИТМ ПОДБОРА ЛОКАЦИЙ ----------------

def get_next_smart_place(user_id: int) -> Optional[Dict[str, Any]]:
    """
    Интеллектуальный моментальный подбор следующей локации:
    1. Исключает просмотренные (лайкнутые, пропущенные и посещенные).
    2. Вычисляет реальное расстояние от выбранного метро / точки пользователя.
    3. Строго фильтрует по дистанции (до 20, 30, 40, 50, до 60 км).
    4. Подбирает места под выбранное время суток (Ночь, Закат, День, Утро),
       отбирая самые красивые для этого периода и проверяя их доступность (ночью — только 24/7 свободный въезд).
    5. Учитывает географическое направление.
    6. Ранжирует с приоритетом секретным спотам, авто-чиллу и вкусам пользователя.
    """
    # 1. Быстро получаем реакции пользователя из БД
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT place_id, action FROM user_reactions WHERE user_id = ?", (user_id,))
        reactions = cursor.fetchall()
        seen_ids = {r["place_id"] for r in reactions}
        
        cursor.execute("""
            SELECT p.category, COUNT(*) as cnt FROM user_reactions r
            JOIN places p ON r.place_id = p.id
            WHERE r.user_id = ? AND r.action = 'liked'
            GROUP BY p.category
        """, (user_id,))
        cat_likes = {r["category"]: r["cnt"] for r in cursor.fetchall()}

    settings = get_user_settings(user_id)
    start_pt = get_user_start_point(user_id)
    start_lat = start_pt["lat"]
    start_lon = start_pt["lon"]
    
    time_filter = settings["time_filter"]
    zone_filter = settings["zone_filter"]
    
    try:
        max_dist_km = float(settings["distance_filter"])
    except Exception:
        max_dist_km = 60.0

    all_places = get_all_places_from_db()

    # Фильтрация кандидатов
    candidates: List[Dict[str, Any]] = []
    for p in all_places:
        if p["id"] in seen_ids:
            continue
            
        p_lat = p.get("lat", 60.0371)
        p_lon = p.get("lon", 30.3162)
        dist_km = haversine_distance_km(start_lat, start_lon, p_lat, p_lon)
        
        # Строгий фильтр по расстоянию от выбранного метро!
        if dist_km > max_dist_km:
            continue
            
        # Фильтр по направлению
        if zone_filter != "all_north" and p.get("zone") != zone_filter:
            continue
            
        best_times = p.get("best_times_list") or ["night", "sunset", "day", "morning"]
        vibe_lower = (p.get("vibe", "") + " " + p.get("night_vibe", "")).lower()
        is_24_7 = p.get("is_24_7", 1)
        
        # Подбор под время суток и доступность
        if time_filter == "night":
            # Ночью: проверяем круглосуточную доступность 24/7 (въезд открыт) И красоту ночью!
            if not is_24_7:
                continue
            if "night" not in best_times and not p.get("car_chill") and not p.get("night_vibe"):
                continue
        elif time_filter == "sunset":
            # Вечер / закат: локации с открытым горизонтом, водой или смотровыми
            if "sunset" not in best_times and p.get("category") not in ("coast", "viewpoint") and "закат" not in vibe_lower:
                continue
        elif time_filter == "day":
            # День: экотропы, парки, природа, архитектура
            if "day" not in best_times and p.get("category") not in ("nature", "architecture", "coast"):
                continue
        elif time_filter == "morning":
            # Утро / рассвет: утренний туман, озера, Ладога (восход солнца), тишина
            if "morning" not in best_times and "рассвет" not in vibe_lower and p.get("zone") != "vsevolozhsk_ladoga" and p.get("category") != "nature":
                continue

        # Создаем обогащенную копию
        cand = dict(p)
        cand["distance_km"] = dist_km
        cand["car_time"] = estimate_car_time(dist_km)
        cand["start_name"] = start_pt["name"]
        cand["yandex_map"] = f"https://yandex.ru/maps/?rtext={start_lat},{start_lon}~{p_lat},{p_lon}&rtt=auto"
        candidates.append(cand)
        
    if not candidates:
        return None

    # Умное ранжирование (секретные точки, авто-чилл, предпочтения пользователя)
    def score_place(place: Dict[str, Any]) -> float:
        score = 10.0
        
        pop = place.get("popularity", "hidden")
        if pop == "secret":
            score += 25.0
        elif pop == "hidden":
            score += 15.0
            
        if place.get("car_chill", 0) == 1:
            score += 20.0
            
        # Бонусы под текущий фильтр времени
        if time_filter == "night":
            if place.get("category") == "night_chill":
                score += 22.0
            elif place.get("category") == "viewpoint":
                score += 15.0
        elif time_filter == "sunset":
            if place.get("category") in ("coast", "viewpoint"):
                score += 25.0
        elif time_filter == "morning":
            if place.get("zone") == "vsevolozhsk_ladoga" or "рассвет" in place.get("vibe", "").lower():
                score += 30.0
        elif time_filter == "day":
            if place.get("category") in ("nature", "architecture"):
                score += 20.0
                
        # Бонус за любимые категории пользователя
        score += cat_likes.get(place.get("category", ""), 0) * 3.0
        
        # Небольшая вариативность, чтобы не было скучно
        score *= random.uniform(0.92, 1.12)
        return score

    candidates.sort(key=score_place, reverse=True)
    return candidates[0]
