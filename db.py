# -*- coding: utf-8 -*-
import sqlite3
import os
import random
import logging
from typing import List, Dict, Optional, Tuple, Any
from places_data import PLACES, CATEGORY_NAMES, DISTANCE_GROUPS

logger = logging.getLogger(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), "bot_data.db")


def get_connection():
    return sqlite3.connect(DB_PATH)


def init_db():
    """Инициализация таблиц базы данных и синхронизация локаций."""
    with get_connection() as conn:
        cursor = conn.cursor()
        
        # 1. Таблица локаций (динамическая база данных)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS places (
                id TEXT PRIMARY KEY,
                title TEXT,
                category TEXT,
                distance_km INTEGER,
                car_time TEXT,
                range_group TEXT,
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
        
        # 2. Таблица реакций (лайк/пропуск)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_reactions (
                user_id INTEGER,
                place_id TEXT,
                action TEXT, -- 'liked' или 'skipped'
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, place_id)
            )
        """)
        
        # 3. Таблица настроек пользователя (фильтр расстояния и т.д.)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                distance_filter TEXT DEFAULT 'all',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        conn.commit()

    # Синхронизируем базовые места из places_data.py при старте
    sync_places_from_catalog()


def sync_places_from_catalog() -> int:
    """
    Синхронизирует локации из встроенного каталога в таблицу places.
    Возвращает количество добавленных новых мест.
    """
    added_count = 0
    with get_connection() as conn:
        cursor = conn.cursor()
        for p in PLACES:
            cursor.execute("SELECT id FROM places WHERE id = ?", (p["id"],))
            if not cursor.fetchone():
                cursor.execute("""
                    INSERT INTO places (
                        id, title, category, distance_km, car_time, range_group,
                        vibe, season, time_of_day, car_access, photo_tips, yandex_map, is_custom
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """, (
                    p["id"], p["title"], p["category"], p["distance_km"],
                    p["car_time"], p["range_group"], p["vibe"], p["season"],
                    p["time_of_day"], p["car_access"], p["photo_tips"], p["yandex_map"]
                ))
                added_count += 1
        conn.commit()
    if added_count > 0:
        logger.info(f"Синхронизировано новых локаций: {added_count}")
    return added_count


def get_all_places_from_db() -> List[Dict[str, Any]]:
    """Получить все активные локации из базы данных SQLite."""
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, category, distance_km, car_time, range_group,
                   vibe, season, time_of_day, car_access, photo_tips, yandex_map, is_custom
            FROM places
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
            SELECT id, title, category, distance_km, car_time, range_group,
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
                    id, title, category, distance_km, car_time, range_group,
                    vibe, season, time_of_day, car_access, photo_tips, yandex_map, is_custom
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    vibe = excluded.vibe,
                    yandex_map = excluded.yandex_map
            """, (
                place_dict["id"], place_dict["title"], place_dict["category"],
                place_dict.get("distance_km", 40), place_dict.get("car_time", "30-40 мин"),
                place_dict.get("range_group", "50"), place_dict["vibe"],
                place_dict.get("season", "Круглый год"), place_dict.get("time_of_day", "День/закат"),
                place_dict.get("car_access", "Доступен подъезд на авто"),
                place_dict.get("photo_tips", "Видовые кадры природы и архитектуры"),
                place_dict.get("yandex_map", "https://yandex.ru/maps/")
            ))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Ошибка при добавлении места: {e}")
            return False


# Пул дополнительных уникальных секретных локаций для авто-дополнения базы
AUTO_DISCOVERY_POOL = [
    {
        "id": "fort_krasnaya_gorka",
        "title": "Форт «Красная Горка» и артиллерийские транспортеры",
        "category": "architecture",
        "distance_km": 74,
        "car_time": "1 час по КАД через Кронштадт",
        "range_group": "80",
        "vibe": "Гигантские 305-мм морские орудия на железнодорожных транспортерах, сосновый бор на обрыве над заливом.",
        "season": "Круглый год.",
        "time_of_day": "День или закат.",
        "car_access": "Асфальт через поселок Лебяжье прямо до форта. Проезд свободный.",
        "photo_tips": "Огромные пушки на рельсах, старинные пороховые погреба среди песчаных дюн.",
        "yandex_map": "https://yandex.ru/maps/?pt=29.333333,59.973611&z=15&l=map"
    },
    {
        "id": "taitsy_demidov_estate",
        "title": "Заброшенная усадьба Демидовых в Тайцах",
        "category": "architecture",
        "distance_km": 52,
        "car_time": "50 мин по Киевскому ш.",
        "range_group": "80",
        "vibe": "Готический колодец-башня, Горбатый мост, вековые дубы и таинственный каменный дворец в романтическом парке.",
        "season": "Осень и весна.",
        "time_of_day": "Предзакатный час.",
        "car_access": "Асфальт до поселка Тайцы, парковка у входа в парк.",
        "photo_tips": "Готический колодец из серого камня, отражения старинных мостиков в пруду.",
        "yandex_map": "https://yandex.ru/maps/?pt=30.121944,59.658889&z=15&l=map"
    },
    {
        "id": "ragusha_canyon",
        "title": "Каньон реки Рагуша и исчезающая река",
        "category": "nature",
        "distance_km": 140,
        "car_time": "2 часа по Мурманскому шоссе",
        "range_group": "100+",
        "vibe": "Уникальный природный каньон карстового происхождения: река уходит под землю, обнажая сухое каменистое русло с водопадами.",
        "season": "Май — октябрь.",
        "time_of_day": "Полдень.",
        "car_access": "Трасса на Тихвин, далее через Бокситогорск. Доступно для легковых авто.",
        "photo_tips": "Отвесные скальные стены каньона, дикие карстовые воронки и пороги.",
        "yandex_map": "https://yandex.ru/maps/?pt=33.951389,59.288889&z=15&l=map"
    },
    {
        "id": "krasavitsa_lake",
        "title": "Озеро Красавица (Большое Симагинское) в Ильичево",
        "category": "lakes",
        "distance_km": 44,
        "car_time": "40 мин по трассе «Скандинавия»",
        "range_group": "50",
        "vibe": "Высокие сосновые обрывы, чистейшая вода и золотистый песок. Одно из самых романтичных озер перешейка.",
        "season": "Лето (купание и сапборды), осень (сосновый воздух и прогулки).",
        "time_of_day": "Закат над озером.",
        "car_access": "Асфальт до поселка Ильичево, подъезды прямо к прибрежному сосняку.",
        "photo_tips": "Панорама с высокого соснового утеса на зеркало воды.",
        "yandex_map": "https://yandex.ru/maps/?pt=29.788889,60.258889&z=15&l=map"
    },
    {
        "id": "gladyshevsky_reserve",
        "title": "Заказник «Гладышевский» и старая финская мельница",
        "category": "nature",
        "distance_km": 53,
        "car_time": "45 мин по Приморскому шоссе",
        "range_group": "80",
        "vibe": "Буйная карельская река Гладышевка с редкими жемчужницами, руины каменной финской мельницы и вековые ели.",
        "season": "Весна и золотая осень.",
        "time_of_day": "Дневной рассеянный свет.",
        "car_access": "Асфальт по Приморскому ш. до поселка Серово, стоянка у экотропы.",
        "photo_tips": "Каменные замшелые стены старой мельницы среди бурного потока воды.",
        "yandex_map": "https://yandex.ru/maps/?pt=29.561944,60.198889&z=15&l=map"
    },
    {
        "id": "rodon_lopuhinka",
        "title": "Радоновые озера и каньон в Лопухинке",
        "category": "nature",
        "distance_km": 68,
        "car_time": "55 мин по КАД через Гостилицы",
        "range_group": "80",
        "vibe": "Изумрудно-бирюзовая целебная вода, питаемая чистыми подземными радоновыми ключами, каньон и старая усадьба.",
        "season": "Круглый год. Вода не замерзает даже зимой и сохраняет сказочный цвет.",
        "time_of_day": "Ясный солнечный день.",
        "car_access": "Асфальт до деревни Лопухинка, удобная парковка у спуска к озерам.",
        "photo_tips": "Деревянные мостки вдоль изумрудных озер, купель и родники.",
        "yandex_map": "https://yandex.ru/maps/?pt=29.418889,59.738889&z=15&l=map"
    },
    {
        "id": "baryshevo_bridge",
        "title": "Подвесной мост через Вуоксу и мыс Черемуховый",
        "category": "nature",
        "distance_km": 86,
        "car_time": "1 час 15 мин по трассе «Скандинавия»",
        "range_group": "100+",
        "vibe": "Качающийся подвесной мостик над широким плесом Вуоксы, гранитные скалы и тихие сосновые бухты.",
        "season": "С мая по октябрь.",
        "time_of_day": "Закатный 'золотой час'.",
        "car_access": "Асфальт до поселка Барышево, далее 500 м лесной грунтовки.",
        "photo_tips": "Атмосферный кадр прямо с середины качающегося моста над водной гладью.",
        "yandex_map": "https://yandex.ru/maps/?pt=29.578889,60.698889&z=15&l=map"
    },
    {
        "id": "vysokinskoe_lake",
        "title": "Озеро Высокинское и сосновая дюна у залива",
        "category": "lakes",
        "distance_km": 77,
        "car_time": "1 час 10 мин по Приморскому шоссе",
        "range_group": "80",
        "vibe": "Уникальное место, где чистейшее пресное озеро отделено от Финского залива лишь узкой полосой сосновых дюн.",
        "season": "Лето и ранняя осень.",
        "time_of_day": "Закат на заливе и рассвет на озере.",
        "car_access": "Приморское шоссе, съезды в сосновый бор к пляжам.",
        "photo_tips": "Песчаная коса, поросшая стройными соснами, вид на открытое море.",
        "yandex_map": "https://yandex.ru/maps/?pt=28.868889,60.308889&z=15&l=map"
    }
]


def auto_update_and_expand_places() -> Tuple[int, int]:
    """
    Автоматическое обновление и дополнение базы данных локаций.
    Синхронизирует текущие места и подгружает новые локации из пула открытий.
    Возвращает: (добавлено_новых, всего_в_базе)
    """
    init_db()
    added_from_catalog = sync_places_from_catalog()
    
    # Добавляем новые споты из пула авто-открытий
    added_from_pool = 0
    with get_connection() as conn:
        cursor = conn.cursor()
        for p in AUTO_DISCOVERY_POOL:
            cursor.execute("SELECT id FROM places WHERE id = ?", (p["id"],))
            if not cursor.fetchone():
                cursor.execute("""
                    INSERT INTO places (
                        id, title, category, distance_km, car_time, range_group,
                        vibe, season, time_of_day, car_access, photo_tips, yandex_map, is_custom
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """, (
                    p["id"], p["title"], p["category"], p["distance_km"],
                    p["car_time"], p["range_group"], p["vibe"], p["season"],
                    p["time_of_day"], p["car_access"], p["photo_tips"], p["yandex_map"]
                ))
                added_from_pool += 1
        conn.commit()

        # Считаем общее число локаций в базе
        cursor.execute("SELECT COUNT(*) FROM places")
        total_in_db = cursor.fetchone()[0]

    total_added = added_from_catalog + added_from_pool
    logger.info(f"Авто-обновление базы: добавлено {total_added}, всего теперь {total_in_db}")
    return total_added, total_in_db


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
    """Удалить локацию из избранного."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE user_reactions
            SET action = 'skipped'
            WHERE user_id = ? AND place_id = ?
        """, (user_id, place_id))
        conn.commit()


def get_user_favorites(user_id: int) -> List[Dict[str, Any]]:
    """Получить список всех избранных локаций пользователя из базы."""
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
        
        cursor.execute("""
            SELECT p.category FROM user_reactions r
            JOIN places p ON r.place_id = p.id
            WHERE r.user_id = ? AND r.action = 'liked'
        """, (user_id,))
        categories = [r[0] for r in cursor.fetchall()]

        cursor.execute("SELECT COUNT(*) FROM places")
        total_available = cursor.fetchone()[0]
    
    liked_count = counts.get("liked", 0)
    skipped_count = counts.get("skipped", 0)
    total_seen = liked_count + skipped_count
    
    cat_likes: Dict[str, int] = {}
    for cat in categories:
        cat_likes[cat] = cat_likes.get(cat, 0) + 1
            
    top_cat = None
    if cat_likes:
        top_cat_key = max(cat_likes, key=cat_likes.get)
        top_cat = CATEGORY_NAMES.get(top_cat_key, top_cat_key)
        
    return {
        "total_seen": total_seen,
        "liked_count": liked_count,
        "skipped_count": skipped_count,
        "total_available": total_available,
        "top_category": top_cat
    }


def get_next_smart_place(user_id: int) -> Optional[Dict[str, Any]]:
    """
    Умный подбор следующей непросмотренной локации из базы данных:
    1. Исключает уже просмотренные (лайкнутые или пропущенные).
    2. Учитывает выбранный пользователем радиус расстояния.
    3. Ранжирует на основе предпочтений: отдает предпочтение категориям,
       которые пользователь чаще всего лайкал!
    """
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Получаем реакции пользователя
        cursor.execute("SELECT place_id, action FROM user_reactions WHERE user_id = ?", (user_id,))
        reactions = cursor.fetchall()
        seen_ids = {r["place_id"] for r in reactions}
        
        # Подсчет лайков по категориям
        cursor.execute("""
            SELECT p.category, COUNT(*) as cnt FROM user_reactions r
            JOIN places p ON r.place_id = p.id
            WHERE r.user_id = ? AND r.action = 'liked'
            GROUP BY p.category
        """, (user_id,))
        cat_likes = {r["category"]: r["cnt"] for r in cursor.fetchall()}

    all_places = get_all_places_from_db()
    dist_filter = get_user_distance_filter(user_id)
    
    # Кандидаты (непросмотренные)
    candidates = [p for p in all_places if p["id"] not in seen_ids]
    
    # Применяем фильтр расстояния, если он не 'all'
    if dist_filter != "all":
        candidates = [p for p in candidates if p["range_group"] == dist_filter]
        
    if not candidates:
        return None
        
    def score_place(place: Dict[str, Any]) -> float:
        cat_score = cat_likes.get(place["category"], 0) * 2 + 1
        random_factor = random.uniform(0.8, 1.5)
        return cat_score * random_factor

    candidates.sort(key=score_place, reverse=True)
    return candidates[0]
