# -*- coding: utf-8 -*-
import sqlite3
import os
import random
from typing import List, Dict, Optional, Tuple, Any
from places_data import PLACES, CATEGORY_NAMES

DB_PATH = os.path.join(os.path.dirname(__file__), "bot_data.db")


def get_connection():
    return sqlite3.connect(DB_PATH)


def init_db():
    """Инициализация таблиц базы данных."""
    with get_connection() as conn:
        cursor = conn.cursor()
        
        # Таблица реакций (лайк/пропуск)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_reactions (
                user_id INTEGER,
                place_id TEXT,
                action TEXT, -- 'liked' или 'skipped'
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, place_id)
            )
        """)
        
        # Таблица настроек пользователя (фильтр расстояния и т.д.)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                distance_filter TEXT DEFAULT 'all',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        conn.commit()


def set_user_distance_filter(user_id: int, distance_filter: str):
    """Установить фильтр по расстоянию для пользователя."""
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
    """Получить текущий фильтр по расстоянию."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT distance_filter FROM user_settings WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row[0] if row else "all"


def record_reaction(user_id: int, place_id: str, action: str):
    """Записать реакцию: 'liked' (в избранное) или 'skipped' (пропуск)."""
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
    """Удалить локацию из избранного (перевести в статус skipped)."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE user_reactions
            SET action = 'skipped'
            WHERE user_id = ? AND place_id = ?
        """, (user_id, place_id))
        conn.commit()


def get_user_favorites(user_id: int) -> List[Dict[str, Any]]:
    """Получить список всех избранных локаций пользователя."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT place_id FROM user_reactions
            WHERE user_id = ? AND action = 'liked'
            ORDER BY created_at DESC
        """, (user_id,))
        rows = cursor.fetchall()
        liked_ids = {r[0] for r in rows}
    
    # Возвращаем объекты мест в порядке добавления
    ordered_places = []
    for r in rows:
        place_id = r[0]
        place = next((p for p in PLACES if p["id"] == place_id), None)
        if place:
            ordered_places.append(place)
    return ordered_places


def reset_user_history(user_id: int):
    """Полный сброс истории: удаление всех лайков, пропусков и сброс фильтров."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM user_reactions WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))
        conn.commit()


def get_user_stats(user_id: int) -> Dict[str, Any]:
    """Получить статистику пользователя по просмотренным и любимым категориям."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT action, COUNT(*) FROM user_reactions
            WHERE user_id = ?
            GROUP BY action
        """, (user_id,))
        counts = dict(cursor.fetchall())
        
        # Получаем любимые категории из лайков
        cursor.execute("""
            SELECT place_id FROM user_reactions
            WHERE user_id = ? AND action = 'liked'
        """, (user_id,))
        liked_ids = [r[0] for r in cursor.fetchall()]
    
    liked_count = counts.get("liked", 0)
    skipped_count = counts.get("skipped", 0)
    total_seen = liked_count + skipped_count
    
    cat_likes: Dict[str, int] = {}
    for pid in liked_ids:
        place = next((p for p in PLACES if p["id"] == pid), None)
        if place:
            cat = place["category"]
            cat_likes[cat] = cat_likes.get(cat, 0) + 1
            
    top_cat = None
    if cat_likes:
        top_cat_key = max(cat_likes, key=cat_likes.get)
        top_cat = CATEGORY_NAMES.get(top_cat_key, top_cat_key)
        
    return {
        "total_seen": total_seen,
        "liked_count": liked_count,
        "skipped_count": skipped_count,
        "total_available": len(PLACES),
        "top_category": top_cat
    }


def get_next_smart_place(user_id: int) -> Optional[Dict[str, Any]]:
    """
    Умный подбор следующей непросмотренной локации:
    1. Исключает уже просмотренные (лайкнутые или пропущенные).
    2. Учитывает выбранный пользователем радиус расстояния.
    3. Ранжирует на основе предпочтений: отдает предпочтение категориям,
       которые пользователь чаще всего лайкал в избранное!
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        # Просмотренные места
        cursor.execute("SELECT place_id, action FROM user_reactions WHERE user_id = ?", (user_id,))
        reactions = cursor.fetchall()
        seen_ids = {r[0] for r in reactions}
        
        # Подсчет лайков по категориям для умной выдачи
        liked_ids = [r[0] for r in reactions if r[1] == "liked"]
        
    category_weights: Dict[str, int] = {}
    for pid in liked_ids:
        p = next((x for x in PLACES if x["id"] == pid), None)
        if p:
            category_weights[p["category"]] = category_weights.get(p["category"], 0) + 2

    # Получаем текущий фильтр расстояния
    dist_filter = get_user_distance_filter(user_id)
    
    # Кандидаты (непросмотренные)
    candidates = [p for p in PLACES if p["id"] not in seen_ids]
    
    # Применяем фильтр расстояния, если он не 'all'
    if dist_filter != "all":
        candidates = [p for p in candidates if p["range_group"] == dist_filter]
        
    if not candidates:
        return None
        
    # Считаем вес каждого кандидата (на основе совпадения с любимыми категориями)
    # Добавляем случайный шум, чтобы лента не была жесткой и выдавала разнообразие
    def score_place(place: Dict[str, Any]) -> float:
        cat_score = category_weights.get(place["category"], 1)
        # Рандомный вес от 0.5 до 1.5 для приятной динамики
        random_factor = random.uniform(0.8, 1.5)
        return cat_score * random_factor

    candidates.sort(key=score_place, reverse=True)
    return candidates[0]
