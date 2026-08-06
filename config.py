"""Настройки бота. Все значения берутся из .env (см. .env.example)."""
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

# Крупные вьетнамские хабы, через которые может быть выгоднее лететь, чем
# напрямую между origins и destinations: один из перелётов (какой именно —
# зависит от направления, см. bot.py::_search_via_hub) идёт через один из
# этих городов. Код -> человекочитаемое имя для сообщений.
HUBS: dict[str, str] = {
    "SGN": "Хошимин",
    "HAN": "Ханой",
    "DAD": "Дананг",
}

# Аэропорты вылета (код -> имя в родительном падеже, для фраз вида "из Москвы").
ORIGIN_NAMES: dict[str, str] = {
    "MOW": "Москвы",
    "MRV": "Минеральных Вод",
    "CXR": "Нячанга",
}

# Аэропорты назначения (код -> имя в винительном падеже, для фраз вида "в Москву").
DESTINATION_NAMES: dict[str, str] = {
    "MOW": "Москву",
    "MRV": "Минеральные Воды",
    "KRR": "Краснодар",
    "ASF": "Астрахань",
    "CXR": "Нячанг",
}

# Багаж в эконом-тарифе по умолчанию — это ОБЩЕЕ ЗНАНИЕ о бизнес-модели
# перевозчика (лоукостер vs полносервисный), а НЕ данные из API: у
# Travelpayouts Data API нет ни одного поля про багаж ни в v1/prices/calendar,
# ни в v2/prices/latest (проверено на реальных ответах). Поэтому это
# ориентир, а не факт — у любого перевозчика бывают разные тарифные классы,
# всегда сверяйся на странице бронирования.
AIRLINE_BAGGAGE_INCLUDED: dict[str, bool] = {
    # обычно включён (полносервисные перевозчики)
    "SU": True, "S7": True, "VN": True, "QH": True, "TK": True, "EK": True,
    "QR": True, "SQ": True, "TG": True, "KE": True, "OZ": True, "CZ": True,
    "MU": True, "CA": True, "HU": True, "MF": True, "U6": True, "UL": True,
    # обычно НЕ включён (лоукостеры, базовый тариф)
    "VJ": False, "BL": False, "VZ": False, "FD": False, "AK": False,
    "3K": False, "TR": False, "QZ": False,
}


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    travelpayouts_token: str
    travelpayouts_marker: str | None

    origins: tuple[str, ...] = ("MOW", "MRV")  # обычно один аэропорт вылета на каждое направление, но можно несколько
    # Города назначения В ПОРЯДКЕ ПРИОРИТЕТА (первый — самый желанный). Если
    # несколько вариантов оказались в пределах priority_margin_rub друг от
    # друга по цене — предпочитаем тот, что раньше в этом списке (см.
    # bot.py::_apply_priority), а не просто самый дешёвый.
    destinations: tuple[str, ...] = ("CXR",)
    currency: str = "rub"
    trip_class: int = 0          # 0 = эконом

    # Поиск через вьетнамские хабы (config.HUBS) имеет смысл, только когда
    # маршрут связан с Вьетнамом. Для остальных направлений (например,
    # внутренний перелёт MRV->MOW) выключаем — иначе бот попытается искать
    # несуществующие маршруты вида "MRV -> Хошимин -> Москва".
    enable_hub_search: bool = True

    # Все трое считаются взрослыми пассажирами: детям 13 и 17 лет, а детский
    # тариф и в РФ, и у большинства авиакомпаний действует примерно до 12 лет.
    # adults=3/children=0, а не adults=1/children=2 — иначе ссылка на
    # бронирование кодировала бы "1 взрослый + 2 ребёнка" и Aviasales мог бы
    # показать неверную цену/состав пассажиров при переходе по ней.
    adults: int = 3
    children: int = 0
    infants: int = 0

    # Если search_start_date/search_end_date заданы — используем их как
    # фиксированный период поиска (конкретная поездка), а не окно
    # "ближайшие search_window_days дней от сегодня". start подрезается по
    # max(start, сегодня) на месте использования (bot.py), чтобы не искать
    # билеты в уже прошедшую дату по мере того, как идёт время.
    search_start_date: date | None = None
    search_end_date: date | None = None
    search_window_days: int = 21

    # На сколько дней ДАЛЬШЕ основного окна поиска (search_end_date/
    # search_window_days) фоново забираем цены — ТОЛЬКО чтобы посчитать
    # медиану рынка (market_stats) на более широкой выборке, эти даты НЕ
    # показываются как варианты для покупки. Нужно, чтобы понимать, дорого
    # или дёшево сейчас в пределах основного окна относительно чуть более
    # длинного горизонта.
    market_context_extra_days: int = 10

    check_interval_minutes: int = 20
    default_budget_rub: int = 30_000  # порог для отметки 🔥 — за ОДНОГО человека (сравнивается с ценой за взрослого, не с суммой на всю компанию)
    max_domestic_leg_rub: int = 8_000  # внутренний перелёт по Вьетнаму (нога хаб-маршрута) учитываем, только если дешевле этого

    # Единый порог "почти одинаковая цена — выбираем по приоритету, а не по
    # минимальной цене" (bot.py::_apply_priority). Применяется в двух местах:
    # 1) прямой рейс vs рейс через хаб — прямой предпочитаем, если он дороже
    #    самого дешёвого варианта не больше чем на эту сумму на человека;
    # 2) выбор между несколькими городами назначения (destinations) — более
    #    приоритетный (раньше в списке) город предпочитаем на тех же условиях.
    priority_margin_rub: float = 10_000.0

    # v2/prices/latest (источник цен) не отдаёт время рейсов, но v1/prices/
    # calendar отдаёт departure_at по датам для большинства маршрутов (кроме
    # цены — та ненадёжна для отдельных редких пар, см. travelpayouts.py).
    # Поэтому для рейсов через хаб бот пробует посчитать реальный запас в
    # часах между прилётом первой ноги (departure_at + duration) и вылетом
    # второй; если время найдено — требует min_hub_layover_hours. Если по
    # какой-то дате время не нашлось в календаре — используется запасной
    # вариант: минимум min_hub_layover_days суток (и такой вариант явно
    # помечается в сообщении как неподтверждённый по времени).
    min_hub_layover_hours: int = 6
    min_hub_layover_days: int = 1
    max_hub_layover_days: int = 3

    data_dir: Path = BASE_DIR / "data"

    @property
    def total_passengers(self) -> int:
        return self.adults + self.children


def load_settings() -> Settings:
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    travelpayouts_token = os.environ.get("TRAVELPAYOUTS_TOKEN")
    if not telegram_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в .env — см. README.md")
    if not travelpayouts_token:
        raise RuntimeError("TRAVELPAYOUTS_TOKEN не задан в .env — см. README.md")

    raw_start = os.environ.get("SEARCH_START_DATE", "").strip()
    raw_end = os.environ.get("SEARCH_END_DATE", "").strip()

    return Settings(
        telegram_token=telegram_token,
        travelpayouts_token=travelpayouts_token,
        travelpayouts_marker=os.environ.get("TRAVELPAYOUTS_MARKER") or None,
        origins=tuple(o.strip() for o in os.environ.get("ORIGINS", "MOW,MRV").split(",") if o.strip()),
        destinations=tuple(d.strip() for d in os.environ.get("DESTINATIONS", "CXR").split(",") if d.strip()),
        currency=os.environ.get("CURRENCY", "rub"),
        enable_hub_search=os.environ.get("ENABLE_HUB_SEARCH", "true").strip().lower() not in ("0", "false", "no"),
        search_start_date=date.fromisoformat(raw_start) if raw_start else None,
        search_end_date=date.fromisoformat(raw_end) if raw_end else None,
        search_window_days=int(os.environ.get("SEARCH_WINDOW_DAYS", 21)),
        check_interval_minutes=int(os.environ.get("CHECK_INTERVAL_MINUTES", 20)),
        default_budget_rub=int(os.environ.get("DEFAULT_BUDGET_RUB", 30_000)),
        max_domestic_leg_rub=int(os.environ.get("MAX_DOMESTIC_LEG_RUB", 8_000)),
        priority_margin_rub=float(os.environ.get("PRIORITY_MARGIN_RUB", 10_000.0)),
        market_context_extra_days=int(os.environ.get("MARKET_CONTEXT_EXTRA_DAYS", 10)),
        min_hub_layover_hours=int(os.environ.get("MIN_HUB_LAYOVER_HOURS", 6)),
        min_hub_layover_days=int(os.environ.get("MIN_HUB_LAYOVER_DAYS", 1)),
        max_hub_layover_days=int(os.environ.get("MAX_HUB_LAYOVER_DAYS", 3)),
    )
