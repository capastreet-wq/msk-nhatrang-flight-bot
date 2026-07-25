"""Настройки бота. Все значения берутся из .env (см. .env.example)."""
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

# Крупные вьетнамские хабы, через которые может быть выгоднее лететь, чем
# напрямую в CXR: международный перелёт до хаба часто дешевле (больше
# конкуренции авиакомпаний), а внутренний перелёт хаб -> Нячанг у местных
# лоукостеров стоит копейки. Код -> человекочитаемое имя для сообщений.
HUBS: dict[str, str] = {
    "SGN": "Хошимин",
    "HAN": "Ханой",
    "DAD": "Дананг",
}

# Аэропорты вылета (код -> имя в родительном падеже, для фраз вида "из Москвы").
ORIGIN_NAMES: dict[str, str] = {
    "MOW": "Москвы",
    "MRV": "Минеральных Вод",
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

    origins: tuple[str, ...] = ("MOW", "MRV")  # MOW = все аэропорты Москвы (SVO/DME/VKO) одним кодом; MRV = Минеральные Воды
    destination: str = "CXR"     # Cam Ranh — аэропорт, обслуживающий Нячанг (всегда конечная точка)
    currency: str = "rub"
    trip_class: int = 0          # 0 = эконом

    adults: int = 1
    children: int = 2            # 13 и 17 лет — по тарифам авиакомпаний это "взрослые" места (детский тариф обычно до 12 лет)
    infants: int = 0

    # Если search_start_date/search_end_date заданы — используем их как
    # фиксированный период поиска (конкретная поездка), а не окно
    # "ближайшие search_window_days дней от сегодня". start подрезается по
    # max(start, сегодня) на месте использования (bot.py), чтобы не искать
    # билеты в уже прошедшую дату по мере того, как идёт время.
    search_start_date: date | None = None
    search_end_date: date | None = None
    search_window_days: int = 21

    check_interval_minutes: int = 20
    default_budget_rub: int = 30_000  # порог для отметки 🔥 — за ОДНОГО человека (сравнивается с ценой за взрослого, не с суммой на всю компанию)
    max_domestic_leg_rub: int = 8_000  # маршрут через хаб учитываем, только если внутренний перелёт до CXR дешевле этого

    # Хаб-маршрут (через SGN/HAN/DAD) показываем/ставим впереди прямого,
    # только если он реально экономит от hub_min_savings_usd на человека —
    # иначе показываем прямой рейс, даже если хаб чуть дешевле. usd_rub_rate
    # даёт перевод в рубли для сравнения с ценами Data API (те всегда в
    # рублях) — курс приблизительный (ЦБ на 25.07.2026 ~78 ₽/$), обновляй
    # вручную при сильных колебаниях, точность до рубля тут не нужна.
    hub_min_savings_usd: float = 100.0
    usd_rub_rate: float = 78.0

    # v2/prices/latest (источник цен) не отдаёт время рейсов, но v1/prices/
    # calendar отдаёт departure_at по датам для большинства маршрутов (кроме
    # цены — та ненадёжна для MOW-CXR, см. travelpayouts.py). Поэтому для
    # рейсов через хаб бот пробует посчитать реальный запас в часах между
    # прилётом (departure_at международного рейса + duration) и вылетом
    # внутреннего рейса; если время найдено — требует min_hub_layover_hours.
    # Если по какой-то дате время не нашлось в календаре — используется
    # запасной вариант: минимум min_hub_layover_days суток (и такой вариант
    # явно помечается в сообщении как неподтверждённый по времени).
    min_hub_layover_hours: int = 6
    min_hub_layover_days: int = 1
    max_hub_layover_days: int = 3

    data_dir: Path = BASE_DIR / "data"

    @property
    def total_passengers(self) -> int:
        return self.adults + self.children

    @property
    def hub_min_savings_rub(self) -> float:
        return self.hub_min_savings_usd * self.usd_rub_rate


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
        destination=os.environ.get("DESTINATION", "CXR"),
        currency=os.environ.get("CURRENCY", "rub"),
        search_start_date=date.fromisoformat(raw_start) if raw_start else None,
        search_end_date=date.fromisoformat(raw_end) if raw_end else None,
        search_window_days=int(os.environ.get("SEARCH_WINDOW_DAYS", 21)),
        check_interval_minutes=int(os.environ.get("CHECK_INTERVAL_MINUTES", 20)),
        default_budget_rub=int(os.environ.get("DEFAULT_BUDGET_RUB", 30_000)),
        max_domestic_leg_rub=int(os.environ.get("MAX_DOMESTIC_LEG_RUB", 8_000)),
        hub_min_savings_usd=float(os.environ.get("HUB_MIN_SAVINGS_USD", 100.0)),
        usd_rub_rate=float(os.environ.get("USD_RUB_RATE", 78.0)),
        min_hub_layover_hours=int(os.environ.get("MIN_HUB_LAYOVER_HOURS", 6)),
        min_hub_layover_days=int(os.environ.get("MIN_HUB_LAYOVER_DAYS", 1)),
        max_hub_layover_days=int(os.environ.get("MAX_HUB_LAYOVER_DAYS", 3)),
    )
