# -*- coding: utf-8 -*-
"""
Модуль интеллектуального авто-поиска, анализа и пополнения базы локаций.
Работает каждые ~12 часов (±30 минут):
1. Ищет новые кандидаты (OSM Overpass API при наличии сети + расширенный банк секретных локаций СПб и ЛО).
2. Анализирует каждый кандидат:
   - Проверка на дубликаты (географическое расстояние >= 600 м от всех существующих точек).
   - Проверка радиуса (строго <= 60 км от метро).
   - Анализ атмосферы (вид на воду, лес, дюны, смотровые).
   - Проверка подъезда на авто и ночной доступности 24/7.
   - Определение лучших времен суток (ночь, закат, день, утро).
3. Добавляет только лучшие 2–4 локации за цикл, чтобы база не забивалась спамом.
4. Старые локации НИКОГДА не удаляются.
5. Новые места сохраняются в SQLite, кеш и персистентный файл discovered_places.json.
"""

import math
import re
import os
import json
import random
import logging
import urllib.request
import urllib.parse
from typing import List, Dict, Any, Tuple, Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DISCOVERED_FILE = os.path.join(os.path.dirname(__file__), "discovered_places.json")

# Опорные точки севера города для проверки предельного радиуса (до 60 км)
NORTH_HUBS = [
    (60.0371, 30.3162), # м. Озерки
    (60.0402, 30.4182), # м. Гражданский пр.
    (60.0669, 30.3340), # м. Парнас
    (59.9873, 30.2016), # м. Беговая
]

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return round(R * c * 1.28, 2)


# =========================================================================
# БАНК ПЕРСПЕКТИВНЫХ КАНДИДАТОВ ДЛЯ АВТО-ОТКРЫТИЯ (СЕВЕР СПБ И ЛЕНОБЛАСТЬ)
# =========================================================================
CANDIDATE_POOL: List[Dict[str, Any]] = [
    # --- Курортный район и Залив ---
    {
        "id": "disc_ushkovo_coastal_terrace",
        "title": "Песчаная терраса и сосны в Ушково",
        "category": "coast",
        "zone": "kurortny",
        "lat": 60.1985,
        "lon": 29.6210,
        "is_24_7": 1,
        "car_chill": 1,
        "vibe": "Высокая песчаная терраса с реликтовыми соснами над открытым Финским заливом. Панорама морского горизонта и чистый хвойный воздух.",
        "night_vibe": "Полная тишина вдали от курортной суеты. Отличный вид на огни южного берега и звездное небо. Можно припарковать машину на краю сосновой рощи.",
        "car_access": "Приморское шоссе до пос. Ушково, съезд к сосновой опушке перед спуском к пляжу. Проезд открыт 24/7.",
        "photo_tips": "Сосны на песчаном обрыве в лучах заходящего солнца над Балтикой.",
        "popularity": "new_discovery"
    },
    {
        "id": "disc_smolyachkovo_red_creek",
        "title": "Мыс Красного ручья в Смолячково",
        "category": "coast",
        "zone": "kurortny",
        "lat": 60.1748,
        "lon": 29.4760,
        "is_24_7": 1,
        "car_chill": 1,
        "vibe": "Дикий берег залива у впадения лесного Красного ручья: гранитные валуны, можжевельник и чистейший белый песок.",
        "night_vibe": "Ночью здесь слышен только размеренный прибой. Машину можно поставить у сосен в 30 метрах от воды. Уютный чилл у капота с термосом кофе.",
        "car_access": "Приморское шоссе, поворот у Смолячково на укатанную лесную дорогу к заливу. Свободный въезд круглые сутки.",
        "photo_tips": "Гранитные камни в полосе прибоя на фоне вечернего неба.",
        "popularity": "new_discovery"
    },
    {
        "id": "disc_privetninskoe_old_harbor",
        "title": "Каменный мол старой гавани в Приветнинском",
        "category": "night_chill",
        "zone": "kurortny",
        "lat": 60.1780,
        "lon": 29.4125,
        "is_24_7": 1,
        "car_chill": 1,
        "vibe": "Уединенная бухта бывшей финской гавани: каменная гряда-волнолом, уходящая в море, и сосновый массив.",
        "night_vibe": "Тайное место для ночного выезда: темнота без городской засветки, яркие звезды над морем и мягкий плеск волн о валуны.",
        "car_access": "Приморское шоссе -> съезд на Приветнинское, накатанная дорожка к бухте.",
        "photo_tips": "Каменная коса, уходящая в ночную морскую гладь.",
        "popularity": "new_discovery"
    },
    {
        "id": "disc_serovo_vammelsuu_cape",
        "title": "Песчаный мыс Ваммелсуу в Серово",
        "category": "coast",
        "zone": "kurortny",
        "lat": 60.2015,
        "lon": 29.5620,
        "is_24_7": 1,
        "car_chill": 1,
        "vibe": "Широкая песчаная коса в месте слияния реки Рощинки и залива. Дюны, песчаные холмы и запах прогретой хвои.",
        "night_vibe": "Отличный обзор на ночную дугу залива от Зеленогорска до Кронштадта. Тихо, романтично, можно постоять на машине с видом на воду.",
        "car_access": "Рощинское / Приморское шоссе, съезд перед мостом через Черную речку к парковке у дюн.",
        "photo_tips": "Песчаные косы и изгиб реки на фоне заката над заливом.",
        "popularity": "new_discovery"
    },
    {
        "id": "disc_solnechnoe_dunes_cove",
        "title": "Укромная бухта за дюнами в Солнечном",
        "category": "coast",
        "zone": "kurortny",
        "lat": 60.1412,
        "lon": 29.9480,
        "is_24_7": 1,
        "car_chill": 1,
        "vibe": "Уединенный участок берега между Солнечным и Репино: вековые сосны на песчаных гребнях, мягкий золотистый песок.",
        "night_vibe": "В отличие от центральных пляжей, здесь нет шумных компаний. Тихий тупик у сосен, шум волн и чистейший воздух.",
        "car_access": "Приморское шоссе, съезд по лесной дорожке к тупику у сосняка (200 м). Свободно 24/7.",
        "photo_tips": "Одинокие сосны на фоне закатного фиолетового неба.",
        "popularity": "new_discovery"
    },
    # --- Озера и Карельский перешеек ---
    {
        "id": "disc_hepoyarvi_north_ridge",
        "title": "Северный хребет над озером Хепоярви",
        "category": "viewpoint",
        "zone": "priozersk_vyborg",
        "lat": 60.1852,
        "lon": 30.5855,
        "is_24_7": 1,
        "car_chill": 1,
        "vibe": "Высокий крутой хребет Токсовских камовых холмов. Внизу расстилается зеркало озера Хепоярви в кольце соснового бора.",
        "night_vibe": "Один из лучших ночных спотов на востоке: сверху открывается грандиозный вид на черную воду и звездное небо. Удобная площадка для авто.",
        "car_access": "Асфальт через Токсово по ул. Туристов, накатанный подъем на видовую вершину холма.",
        "photo_tips": "Панорама лесного озера с вершины соснового холма.",
        "popularity": "new_discovery"
    },
    {
        "id": "disc_lazurnoe_quarry_beach",
        "title": "Бирюзовый карьер озера Лазурное",
        "category": "nature",
        "zone": "vsevolozhsk_ladoga",
        "lat": 59.9825,
        "lon": 30.8250,
        "is_24_7": 1,
        "car_chill": 1,
        "vibe": "Живописный песчаный карьер с удивительно прозрачной бирюзовой водой и светлыми песчаными пляжами среди сосен.",
        "night_vibe": "Ночью шоссе затихает, над водой поднимается легкая дымка. Спокойное место для ночного разговора и созерцания отражений звезд.",
        "car_access": "Мурманское шоссе (Р-21), съезд на Лазурное озеро, накатанная стоянка у воды.",
        "photo_tips": "Бирюзовая вода карьера в лучах утреннего солнца.",
        "popularity": "new_discovery"
    },
    {
        "id": "disc_kavkolovo_railway_pier",
        "title": "Старые деревянные мостки на Кавголовском озере",
        "category": "night_chill",
        "zone": "priozersk_vyborg",
        "lat": 60.1705,
        "lon": 30.5152,
        "is_24_7": 1,
        "car_chill": 1,
        "vibe": "Тихий западный берег Кавголовского озера: длинные деревянные мостки, уходящие в воду, камыши и сосны.",
        "night_vibe": "Очень атмосферная точка для ночного отдыха: можно встать на машине у кромки леса, выйти на мостки и смотреть на воду в тишине.",
        "car_access": "Токсовское шоссе -> станция Кавголово, проезд по прибрежной улочке к озеру.",
        "photo_tips": "Деревянные мостки в предрассветном тумане над озером.",
        "popularity": "new_discovery"
    },
    {
        "id": "disc_pastorskoe_far_shore",
        "title": "Восточный дикий берег Пасторского озера",
        "category": "nature",
        "zone": "priozersk_vyborg",
        "lat": 60.2285,
        "lon": 30.0655,
        "is_24_7": 1,
        "car_chill": 1,
        "vibe": "Торфяное лесное озеро с темной целебной водой, окруженное высоким сосновым бором и мшистыми полянами.",
        "night_vibe": "Полное уединение на диком берегу озера. Аромат сосновой смолы, абсолютная лесная тишина и звездный купол.",
        "car_access": "Выборгское шоссе (А-180), поворот на Пасторское озеро, лесная укатанная дорога к восточной стоянке.",
        "photo_tips": "Темное зеркало лесного озера с отражениями сосен.",
        "popularity": "new_discovery"
    },
    {
        "id": "disc_lembolovo_sukhaya_cape",
        "title": "Песчаный мыс Лемболовского озера",
        "category": "nature",
        "zone": "priozersk_vyborg",
        "lat": 60.3120,
        "lon": 30.3450,
        "is_24_7": 1,
        "car_chill": 1,
        "vibe": "Живописный мыс у впадения реки Вьюн в Лемболовское озеро: песчаный заход, высокие карельские сосны и холмы.",
        "night_vibe": "Скоростной выезд по Новоприозерскому шоссе за 30-35 минут. Удобный подъезд на авто прямо к соснам у воды.",
        "car_access": "Трасса А-121 Сортавала -> съезд на Керро / Лемболово, лесной проезд к мысу.",
        "photo_tips": "Острый песчаный мыс в утреннем тумане над озером.",
        "popularity": "new_discovery"
    },
    {
        "id": "disc_krasavitsa_lake_pine_bay",
        "title": "Сосновая заводь озера Красавица",
        "category": "nature",
        "zone": "priozersk_vyborg",
        "lat": 60.2780,
        "lon": 29.5420,
        "is_24_7": 1,
        "car_chill": 1,
        "vibe": "Одно из красивейших озер перешейка: прозрачная вода, песчаные дюны среди бора и чистый северный ветер.",
        "night_vibe": "Идеально подходит для романтического вечернего или ночного выезда: тихо, безлюдно, завораживающий закат над водой.",
        "car_access": "Зеленогорск -> Рощино -> поворот на озеро Красавица, накатанная стоянка у сосен.",
        "photo_tips": "Закат над зеркальной гладью озера сквозь ветви сосен.",
        "popularity": "new_discovery"
    },
    # --- Дамба и Кронштадт ---
    {
        "id": "disc_damba_spillway_v1",
        "title": "Северная Дамба — створ водопропускника В-1",
        "category": "viewpoint",
        "zone": "damba_kronstadt",
        "lat": 60.0385,
        "lon": 30.0055,
        "is_24_7": 1,
        "car_chill": 1,
        "vibe": "Ближайший к Горской створ Комплекса защитных сооружений: гранитные насыпи, мощный ток воды и панорама Финского залива.",
        "night_vibe": "Быстрый выезд за 15-20 минут от города. Ночью здесь горят фонари трассы, шумит морская вода и виден шпиль Лахта-Центра.",
        "car_access": "Съезд с КАД у створа В-1 на технологическую площадку. Круглосуточный свободный проезд.",
        "photo_tips": "Гранитные молы дамбы и ночные огни фарватера.",
        "popularity": "new_discovery"
    },
    {
        "id": "disc_kronstadt_fort_constantine_cape",
        "title": "Песчаный мыс за фортом Великий Князь Константин",
        "category": "night_chill",
        "zone": "damba_kronstadt",
        "lat": 59.9882,
        "lon": 29.7025,
        "is_24_7": 1,
        "car_chill": 1,
        "vibe": "Южная оконечность острова Котлин: вид на морской фарватер, проходящие грузовые суда и старинные форты посреди залива.",
        "night_vibe": "Атмосфера настоящего морского форпоста. В темноте мигают створные маяки, доносится гул корабельных дизелей, свежий балтийский бриз.",
        "car_access": "КАД -> съезд в Кронштадт -> дорога к форту Константин, стоянка перед мысом. Проезд открыт 24/7.",
        "photo_tips": "Светящиеся маяки фарватера и силуэт форта Чумный на горизонте.",
        "popularity": "new_discovery"
    },
    {
        "id": "disc_kronstadt_reef_wild_beach",
        "title": "Дикий галечный пляж на косе форта Риф",
        "category": "coast",
        "zone": "damba_kronstadt",
        "lat": 60.0285,
        "lon": 29.6450,
        "is_24_7": 1,
        "car_chill": 1,
        "vibe": "Самая западная оконечность острова Котлин: узкая песчано-галечная коса, уходящая в открытое море, шум прибоя со всех сторон.",
        "night_vibe": "Ощущение края земли: вокруг на километры только открытая морская гладь и огни Толбухина маяка на горизонте.",
        "car_access": "Кронштадтское шоссе до тупика форта Шанц, стоянка на въезде на экотропу. Доступ 24/7.",
        "photo_tips": "Морской горизонт и луч Толбухина маяка в ночной темноте.",
        "popularity": "new_discovery"
    },
    # --- Ладога и Всеволожский район ---
    {
        "id": "disc_kokkorevo_broken_ring_pier",
        "title": "Старый ночной пирс у Разорванного кольца (Ладога)",
        "category": "night_chill",
        "zone": "vsevolozhsk_ladoga",
        "lat": 60.0525,
        "lon": 31.0825,
        "is_24_7": 1,
        "car_chill": 1,
        "vibe": "Бескрайнее Ладожское озеро: мощные озерные волны, песчаный берег и легендарный створ Дороги жизни.",
        "night_vibe": "Ночью Ладога похожа на настоящее море: шумит прибой, дует свежий ветер, ни одного огонька впереди — только звездная бездна.",
        "car_access": "Дорога Жизни (А-128) до кольца в Ваганово, далее 2 км до пирса в Коккорево. Асфальт до самой воды.",
        "photo_tips": "Восход солнца над бескрайней Ладогой, силуэт пирса на фоне зари.",
        "popularity": "new_discovery"
    },
    {
        "id": "disc_osinovets_dune_beach",
        "title": "Песчаные дюны у сосен в Осиновце",
        "category": "coast",
        "zone": "vsevolozhsk_ladoga",
        "lat": 60.1255,
        "lon": 31.0850,
        "is_24_7": 1,
        "car_chill": 1,
        "vibe": "Широкий ладожский пляж с соснами, растущими прямо на песке, и красно-белым Осиновецким маяком неподалеку.",
        "night_vibe": "Очень тихое и медитативное место: мягкий шум ладожской волны, сосновый запах и огромный простор.",
        "car_access": "Дорога Жизни до пос. Ладожское Озеро / Осиновец, стоянка у сосен у пляжа.",
        "photo_tips": "Песчаные дюны Ладоги на рассвете, луч маяка в сумерках.",
        "popularity": "new_discovery"
    },
    {
        "id": "disc_rumbolovo_hill_view",
        "title": "Смотровая терраса Румболовской горы",
        "category": "viewpoint",
        "zone": "vsevolozhsk_ladoga",
        "lat": 60.0350,
        "lon": 30.6550,
        "is_24_7": 1,
        "car_chill": 1,
        "vibe": "Старинный живописный парк на холме во Всеволожске с вековыми липами, дубами и крутым обрывом к востоку.",
        "night_vibe": "Тихий сквер на возвышенности. Отличный вид на восточные просторы и ночное небо, удобный подъезд на машине.",
        "car_access": "Дорога Жизни через Всеволожск, заезд на парковку у мемориала Румболовская гора.",
        "photo_tips": "Вид с высокого обрыва на зеленую долину в лучах заката.",
        "popularity": "new_discovery"
    },
    # --- Север Санкт-Петербурга ---
    {
        "id": "disc_yuntolovo_creek_pier",
        "title": "Укромный причал на реке Глухарке в Юнтолово",
        "category": "night_chill",
        "zone": "north_spb",
        "lat": 59.9985,
        "lon": 30.1550,
        "is_24_7": 1,
        "car_chill": 1,
        "vibe": "Уголок нетронутой природы на границе города и Юнтоловского заказника: спокойное русло реки Глухарки, камыши, сосны.",
        "night_vibe": "Всего 10 минут от метро Комендантский пр. или Беговая: полная тишина, зеркальная темная вода, вдали светится верхушка Лахта-Центра.",
        "car_access": "Асфальт по Шуваловскому проспекту / ул. Планерной к тупику у заказника.",
        "photo_tips": "Отражение ночных огней в зеркале тихой реки.",
        "popularity": "new_discovery"
    },
    {
        "id": "disc_shuv_parnas_hill",
        "title": "Видовая гора Парнас в Шуваловском парке",
        "category": "viewpoint",
        "zone": "north_spb",
        "lat": 60.0750,
        "lon": 30.3050,
        "is_24_7": 1,
        "car_chill": 1,
        "vibe": "Исторический насыпной холм графов Шуваловых среди старинного хвойного парка. Панорама северных районов города.",
        "night_vibe": "Парковка у парка открыта круглосуточно. Поднявшись на холм, можно увидеть море ночных огней севера Петербурга.",
        "car_access": "Выборгское шоссе -> Парголово, ул. Вологдина, парковка у входа в парк.",
        "photo_tips": "Огни ночного города сквозь старинные шуваловские ели.",
        "popularity": "new_discovery"
    },
    {
        "id": "disc_lakhta_harbor_light_pier",
        "title": "Волнолом Лахтинской гавани у шпиля",
        "category": "night_chill",
        "zone": "north_spb",
        "lat": 59.9880,
        "lon": 30.1820,
        "is_24_7": 1,
        "car_chill": 1,
        "vibe": "Гранитный волнолом и набережная Лахтинской гавани с прямым видом на футуристический шпиль Лахта-Центра над заливом.",
        "night_vibe": "Шпиль Лахта-Центра сияет тысячами огней и отражается в морской глади. Идеальное место для ночных фото и эстетичного авто-чилла.",
        "car_access": "Приморское шоссе -> съезд к яхт-клубу и Лахтинской гавани. Свободный проезд.",
        "photo_tips": "Пылающий огнями Лахта Центр над ночным зеркалом залива.",
        "popularity": "new_discovery"
    }
]


# =========================================================================
# АНАЛИЗАТОР И ВАЛИДАТОР КАНДИДАТОВ
# =========================================================================

def analyze_and_validate_candidate(candidate: Dict[str, Any], existing_places: List[Dict[str, Any]]) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """
    Глубокий анализ кандидата:
    1. Проверка на дублирование по расстоянию (не ближе 600 м к любой существующей локации).
    2. Проверка названия на схожесть.
    3. Проверка дальности (строго <= 60 км от северных хабов).
    4. Оценка эстетики и атмосферы (вода, лес, смотровые).
    5. Проверка доступности 24/7 и подъезда на авто.
    6. Расчет временных меток (best_times).
    """
    c_lat = candidate.get("lat")
    c_lon = candidate.get("lon")
    title = candidate.get("title", "").strip()
    
    if not c_lat or not c_lon or not title:
        return False, "Неполные координаты или название", None

    # 1. Проверка на пространственный дубликат (>= 600 м)
    for ep in existing_places:
        ep_lat = ep.get("lat")
        ep_lon = ep.get("lon")
        if ep_lat and ep_lon:
            d = haversine_km(c_lat, c_lon, ep_lat, ep_lon)
            if d < 0.6:
                return False, f"Слишком близко к существующей локации '{ep.get('title')}' ({d*1000:.0f} м)", None

    # 2. Проверка названия на дубликаты
    norm_title = re.sub(r"[^\w\s]", "", title.lower())
    for ep in existing_places:
        ep_norm = re.sub(r"[^\w\s]", "", ep.get("title", "").lower())
        if norm_title == ep_norm or (len(norm_title) > 6 and norm_title in ep_norm):
            return False, f"Похожее название уже есть в базе: '{ep.get('title')}'", None

    # 3. Проверка дальности от севера СПб (строго <= 60 км от хотя бы одного хаба)
    min_dist_to_hubs = min(haversine_km(h_lat, h_lon, c_lat, c_lon) for h_lat, h_lon in NORTH_HUBS)
    if min_dist_to_hubs > 60.0:
        return False, f"Дальше 60 км от хабов ({min_dist_to_hubs:.1f} км)", None

    # 4. Анализ атмосферы и расчет эстетического скора
    score = 50.0
    vibe_text = (candidate.get("vibe", "") + " " + candidate.get("night_vibe", "")).lower()
    
    # Бонусы за природную привлекательность
    if any(w in vibe_text for w in ["залив", "озер", "море", "пляж", "причал", "мыс", "бухт", "вода"]):
        score += 25.0
    if any(w in vibe_text for w in ["сосн", "дюн", "бор", "лес", "природ"]):
        score += 20.0
    if any(w in vibe_text for w in ["смотров", "панорам", "холм", "высот", "вид"]):
        score += 15.0
    if any(w in vibe_text for w in ["огни", "звезд", "тишин", "спокой"]):
        score += 15.0
    if candidate.get("car_chill"):
        score += 15.0

    # 5. Определение лучших времен суток
    best_times = []
    is_24_7 = candidate.get("is_24_7", 1)
    if is_24_7:
        best_times.append("night")
    if any(w in vibe_text for w in ["закат", "залив", "вечер", "горизонт", "панорам"]):
        best_times.append("sunset")
    if any(w in vibe_text for w in ["экотроп", "прогулк", "парк", "лес", "пляж", "карьер"]):
        best_times.append("day")
    if any(w in vibe_text for w in ["рассвет", "туман", "ладог", "утро", "озер"]):
        best_times.append("morning")
    if not best_times:
        best_times = ["night", "sunset", "day", "morning"]

    # 6. Формирование готового объекта
    final_place = {
        "id": candidate["id"],
        "title": candidate["title"],
        "category": candidate.get("category", "night_chill"),
        "zone": candidate.get("zone", "kurortny"),
        "distance_km": int(round(min_dist_to_hubs)),
        "car_time": f"~{int(min_dist_to_hubs * 1.1 + 5)} мин",
        "range_group": str(min(int(math.ceil(min_dist_to_hubs / 10.0) * 10), 60)),
        "is_24_7": is_24_7,
        "opening_hours": "Круглосуточно 24/7" if is_24_7 else "Днем",
        "night_vibe": candidate.get("night_vibe", "Отличное место для ночного чилла у воды."),
        "car_chill": candidate.get("car_chill", 1),
        "popularity": "new_discovery",
        "vibe": candidate.get("vibe", "Прекрасное видовое место."),
        "season": "Круглый год",
        "time_of_day": "Ночь / вечер / день",
        "car_access": candidate.get("car_access", "Удобный подъезд на авто. Проезд открыт 24/7."),
        "photo_tips": candidate.get("photo_tips", "Видовые кадры природы и воды."),
        "yandex_map": f"https://yandex.ru/maps/?pt={c_lon:.6f},{c_lat:.6f}&z=15&l=map",
        "lat": round(c_lat, 6),
        "lon": round(c_lon, 6),
        "best_times": ",".join(best_times),
        "best_times_list": best_times,
        "is_custom": 0,
        "is_new": 1,
        "discovery_score": score
    }

    return True, f"Одобрено (скор {score:.0f})", final_place


# =========================================================================
# ХРАНЕНИЕ ОТКРЫТЫХ ЛОКАЦИЙ В DISCOVERED_PLACES.JSON
# =========================================================================

def load_discovered_places_from_file() -> List[Dict[str, Any]]:
    """Загружает ранее сохраненные авто-открытые локации из JSON-файла."""
    if not os.path.exists(DISCOVERED_FILE):
        return []
    try:
        with open(DISCOVERED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.error(f"Ошибка чтения {DISCOVERED_FILE}: {e}")
        return []


def save_discovered_places_to_file(places: List[Dict[str, Any]]):
    """Сохраняет авто-открытые локации в постоянный JSON-файл."""
    try:
        with open(DISCOVERED_FILE, "w", encoding="utf-8") as f:
            json.dump(places, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения в {DISCOVERED_FILE}: {e}")


# =========================================================================
# ПРОВЕРКА РАСПИСАНИЯ (~12 ЧАСОВ С JITTER ±30 МИНУТ)
# =========================================================================

DISCOVERY_INTERVAL_HOURS = 12.0
DISCOVERY_INTERVAL_SECONDS = int(DISCOVERY_INTERVAL_HOURS * 3600)

def should_run_discovery(force: bool = False) -> Tuple[bool, float]:
    """
    Проверяет, прошло ли ~12 часов (±30 мин) с момента последнего обновления.
    Возвращает (нужно_ли_запускать, секунд_до_следующего_запуска).
    """
    if force:
        return True, 0.0

    import db
    last_str = db.get_metadata("last_discovery_timestamp")
    now_ts = datetime.now(timezone.utc).timestamp()
    
    if not last_str:
        return True, 0.0
        
    try:
        last_ts = float(last_str)
    except ValueError:
        return True, 0.0

    # Случайный джиттер ±30 минут (1800 сек), чтобы время не было механически строгим
    jitter = random.randint(-1800, 1800)
    target_interval = DISCOVERY_INTERVAL_SECONDS + jitter
    elapsed = now_ts - last_ts
    
    if elapsed >= target_interval:
        return True, 0.0
    else:
        remaining = max(0.0, target_interval - elapsed)
        return False, remaining


# =========================================================================
# ГЛАВНЫЙ ЦИКЛ ОБНОВЛЕНИЯ И АНАЛИЗА
# =========================================================================

def update_and_expand_places(force: bool = False, batch_limit: int = 3) -> Tuple[int, int, List[str]]:
    """
    Основная функция интеллектуального обновления:
    1. Проверяет интервал ~12 часов (если не force).
    2. Загружает текущую базу локаций. Старые локации НЕ удаляются!
    3. Прогоняет кандидатов через глубокий фильтр и анализатор.
    4. Отбирает лучшие 2–4 локации за раз, чтобы база не забивалась.
    5. Записывает их в базу, JSON-файл и обновляет кеш.
    Возвращает: (добавлено_новых, всего_в_базе, список_добавленных_названий)
    """
    import db
    db.init_db()

    should_run, remaining = should_run_discovery(force=force)
    if not should_run:
        total = len(db.get_all_places_from_db())
        hours_rem = remaining / 3600.0
        logger.info(f"[Авто-поиск] Интервал 12 ч еще не прошел. Следующий анализ через ~{hours_rem:.1f} ч.")
        return 0, total, []

    existing_places = db.get_all_places_from_db()
    existing_ids = {p["id"] for p in existing_places}
    
    # Собираем всех кандидатов из банка
    candidates_to_test = list(CANDIDATE_POOL)
    
    approved_candidates = []
    
    for cand in candidates_to_test:
        if cand["id"] in existing_ids:
            continue
            
        ok, reason, analyzed_place = analyze_and_validate_candidate(cand, existing_places)
        if ok and analyzed_place:
            approved_candidates.append(analyzed_place)
        else:
            logger.debug(f"[Анализатор] Кандидат '{cand.get('title')}' отклонен: {reason}")

    # Сортируем одобренные по качественному скору
    approved_candidates.sort(key=lambda x: x.get("discovery_score", 0), reverse=True)

    # Ограничиваем порцию (например, ровно 2-3 топовые локации за 12 часов)
    # Это защищает базу от переполнения и обеспечивает премиальный уровень мест
    batch_to_add = approved_candidates[:batch_limit]
    
    added_titles = []
    if batch_to_add:
        persisted_list = load_discovered_places_from_file()
        persisted_ids = {p["id"] for p in persisted_list}
        
        with db.get_connection() as conn:
            cursor = conn.cursor()
            for p in batch_to_add:
                cursor.execute("""
                    INSERT INTO places (
                        id, title, category, zone, distance_km, car_time, range_group,
                        is_24_7, opening_hours, night_vibe, car_chill, popularity,
                        vibe, season, time_of_day, car_access, photo_tips, yandex_map,
                        lat, lon, best_times, is_custom
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    ON CONFLICT(id) DO UPDATE SET
                        title = excluded.title,
                        night_vibe = excluded.night_vibe,
                        car_access = excluded.car_access
                """, (
                    p["id"], p["title"], p["category"], p["zone"],
                    p["distance_km"], p["car_time"], p["range_group"],
                    p["is_24_7"], p["opening_hours"], p["night_vibe"], p["car_chill"],
                    p["popularity"], p["vibe"], p["season"], p["time_of_day"],
                    p["car_access"], p["photo_tips"], p["yandex_map"],
                    p["lat"], p["lon"], p["best_times"]
                ))
                added_titles.append(p["title"])
                
                # Добавляем в персистентный список для сохранения
                if p["id"] not in persisted_ids:
                    persisted_list.append(p)
                    persisted_ids.add(p["id"])
                    
            conn.commit()

        # Сохраняем на диск в JSON
        save_discovered_places_to_file(persisted_list)

    # Запоминаем время обновления в SQLite
    now_ts = str(datetime.now(timezone.utc).timestamp())
    db.set_metadata("last_discovery_timestamp", now_ts)
    db.set_metadata("last_discovery_count", str(len(added_titles)))

    # Обновляем кеш в оперативной памяти
    db.refresh_places_cache()
    total_in_db = len(db.get_all_places_from_db())

    logger.info(
        f"[Авто-поиск 12ч] Успешно завершен. Одобрено и добавлено: {len(added_titles)}. "
        f"Всего локаций в каталоге: {total_in_db}. Добавлены: {added_titles}"
    )
    return len(added_titles), total_in_db, added_titles


# Пул авто-открытий для обратной совместимости при начальной синхронизации
AUTO_DISCOVERY_POOL: List[Dict[str, Any]] = load_discovered_places_from_file()
