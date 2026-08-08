"""Telegram-бот: мониторит цены на авиабилеты (маршрут и даты — в config.py/.env)
и присылает сообщение на каждой проверке, с отметками, когда цена особенно
выгодна. Настройки — в .env, инструкция — в README.md.
"""
import asyncio
import logging
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import AIRLINE_BAGGAGE_INCLUDED, DESTINATION_NAMES, HUBS, ORIGIN_NAMES, Settings, load_settings
from storage import Store
from travelpayouts import TravelpayoutsClient, build_booking_link, get_cheapest_in_window, months_between

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("flight-bot")

settings: Settings = load_settings()
store = Store(settings.data_dir / "state.json")
bot = Bot(token=settings.telegram_token, default=DefaultBotProperties(parse_mode=None))
dp = Dispatcher()
tp_client = TravelpayoutsClient(settings.travelpayouts_token, settings.currency)

_VIETNAM_AIRPORTS = frozenset({"CXR", *HUBS.keys()})  # для определения, какая нога хаб-маршрута внутренняя, а какая международная

ALERT_MEANINGFUL_DROP = 0.97  # в заголовке отмечаем "цена упала", если ниже последней увиденной минимум на 3%
MARKET_BEATING_RATIO = 0.85   # цена <= 85% медианы по текущей выдаче — считаем "заметно дешевле рынка"


def estimate_total_price(single_adult_price: float) -> float:
    """Data API отдаёт цену за 1 взрослого — умножаем на общее число
    пассажиров (settings.total_passengers, все считаются взрослыми, см.
    Settings.adults/children). Это оценка, а не точная цена."""
    return single_adult_price * settings.total_passengers


_PASSENGER_COUNT_WORDS = {1: "одного", 2: "двоих", 3: "троих", 4: "четверых", 5: "пятерых", 6: "шестерых"}


def _total_label() -> str:
    n = settings.total_passengers
    word = _PASSENGER_COUNT_WORDS.get(n)
    return f"на {word}" if word else f"на {n} человек"


def budget_for_chat(chat_id: int) -> int:
    """Порог для отметки 🔥 — в рублях ЗА ЧЕЛОВЕКА (сравнивается напрямую с
    ценой за взрослого, а не с суммой на всю компанию)."""
    chat = store.get_chat(chat_id)
    return chat.get("budget") or settings.default_budget_rub


def market_stats(options: list) -> tuple[float, int]:
    """Медиана цены за взрослого по всем найденным сейчас вариантам (не только
    показанным в топе) — это и есть "плюс-минус средняя цена по рынку" на
    данный момент, посчитанная из реально найденных предложений, а не
    угаданная заранее."""
    prices = [o.total_price for o in options]
    return statistics.median(prices), len(prices)


@dataclass
class Leg:
    origin: str
    destination: str
    date: date
    price: float
    link: str
    airline: str | None = None
    departure_at: datetime | None = None  # локальное время вылета, как на билете (из v1/prices/calendar)


@dataclass
class RouteOption:
    total_price: float  # сумма цен за взрослого по всем ногам маршрута
    legs: list[Leg]
    label: str


def _baggage_hint(airline: str | None) -> str:
    """🧳 + ✅/❌/❓ — ориентир по типу перевозчика (см. config.AIRLINE_BAGGAGE_INCLUDED),
    НЕ данные о конкретном тарифе — Data API багаж вообще не отдаёт."""
    if not airline:
        return "🧳❓"
    included = AIRLINE_BAGGAGE_INCLUDED.get(airline)
    if included is True:
        return "🧳✅"
    if included is False:
        return "🧳❌"
    return "🧳❓"


def _within_transfer_limit(item: dict) -> bool:
    """True, если у билета не больше settings.max_transfers пересадок
    (number_of_changes из сырых данных v2/prices/latest). Отсутствие поля
    трактуем как 0 пересадок (не как "неизвестно, но безопасно исключить")
    — это соответствует наблюдаемому формату ответов API."""
    return (item.get("raw") or {}).get("number_of_changes", 0) <= settings.max_transfers


def _make_leg(origin: str, destination: str, item: dict, meta: dict | None = None) -> Leg:
    link = build_booking_link(
        origin, destination, item["date"],
        settings.adults, settings.children, settings.infants, settings.travelpayouts_marker,
    )
    airline = (meta or {}).get("airline")
    departure_at = (meta or {}).get("departure_at")
    return Leg(
        origin=origin, destination=destination, date=item["date"], price=item["price"], link=link,
        airline=airline, departure_at=departure_at,
    )


async def _get_route_metadata(origin: str, destination: str, dates: set) -> dict:
    """Время вылета и авиакомпания по конкретным датам (см.
    TravelpayoutsClient.get_calendar_metadata) — используется для оценки
    запаса на пересадку и подсказки про багаж, не для цены."""
    if not dates:
        return {}
    merged: dict[str, dict] = {}
    for month in months_between(min(dates), max(dates)):
        merged.update(await tp_client.get_calendar_metadata(origin, destination, month))
    return {d: merged[d.isoformat()] for d in dates if d.isoformat() in merged}


async def _search_direct(origin_code: str, destination_code: str, start: date, end: date) -> list[RouteOption]:
    try:
        direct = await get_cheapest_in_window(tp_client, origin_code, destination_code, start, end)
    except Exception:
        log.exception("Ошибка поиска прямого варианта %s -> %s", origin_code, destination_code)
        return []
    candidates = [c for c in direct if _within_transfer_limit(c)][:3]
    try:
        meta_by_date = await _get_route_metadata(origin_code, destination_code, {c["date"] for c in candidates})
    except Exception:
        log.exception("Ошибка получения метаданных %s -> %s", origin_code, destination_code)
        meta_by_date = {}
    origin_name = ORIGIN_NAMES.get(origin_code, origin_code)
    destination_name = DESTINATION_NAMES.get(destination_code, destination_code)
    options = []
    for item in candidates:
        leg = _make_leg(origin_code, destination_code, item, meta_by_date.get(item["date"]))
        options.append(RouteOption(total_price=leg.price, legs=[leg], label=f"Прямой из {origin_name} в {destination_name}"))
    return options


async def _cheapest_direct_headline() -> str:
    """Первая строка каждого сообщения: самый дешёвый прямой билет (один
    билет, без сборки через хаб — то же значение "прямой", что и у остальных
    RouteOption с label "Прямой из ...") по первому аэропорту вылета
    (settings.origins[0]) и самому приоритетному городу назначения
    (settings.destinations[0]) на ближайшие 3 дня. Отдельный, более узкий
    поиск от основного окна (settings.search_start_date/end) — под
    наблюдение, что горящие предложения появляются за пару-тройку дней до
    вылета."""
    origin = settings.origins[0]
    destination = settings.destinations[0]
    today = date.today()
    near_end = today + timedelta(days=3)
    try:
        raw_candidates = await get_cheapest_in_window(tp_client, origin, destination, today, near_end)
    except Exception:
        log.exception("Ошибка поиска ближайшего прямого рейса %s->%s", origin, destination)
        return f"✈️ Не удалось проверить ближайшие прямые рейсы {origin}→{destination} (ошибка API)\n\n"
    candidates = [c for c in raw_candidates if _within_transfer_limit(c)]
    if not candidates:
        return f"✈️ На ближайшие 3 дня прямых билетов {origin}→{destination} (≤{settings.max_transfers} пересадки) не найдено в кэше Aviasales\n\n"
    best = min(candidates, key=lambda c: c["price"])
    try:
        meta = await _get_route_metadata(origin, destination, {best["date"]})
    except Exception:
        log.exception("Ошибка получения метаданных для ближайшего прямого рейса %s->%s", origin, destination)
        meta = {}
    leg = _make_leg(origin, destination, best, meta.get(best["date"]))
    total_family = estimate_total_price(leg.price)
    return (
        f"✈️ Самый дешёвый прямой {origin}→{destination} на ближайшие 3 дня: "
        f"{_format_leg_datetime(leg)} — {leg.price:.0f} ₽/чел. ≈ {total_family:.0f} ₽ {_total_label()} {_baggage_hint(leg.airline)}\n"
        f"{leg.link}\n\n"
    )


async def _search_via_hub(
    origin_code: str, hub_code: str, hub_name: str, destination_code: str, start: date, end: date,
) -> list[RouteOption]:
    """Подбирает пару билетов origin->hub (leg_a) и hub->destination (leg_b)
    с реальным запасом на пересадку. v1/prices/calendar отдаёт время вылета
    (departure_at) для большинства маршрутов — используем его вместе с
    длительностью рейса (duration из v2/prices/latest), чтобы оценить время
    прилёта в хаб и сравнить с временем вылета второй ноги. Если для
    какой-то даты время не нашлось — используем запасной вариант: минимум
    min_hub_layover_days суток, с явной пометкой в названии варианта, что
    время не подтверждено.

    Какая из двух ног — "длинная международная" (только прямой рейс,
    number_of_changes == 0), а какая "короткая внутренняя по Вьетнаму"
    (ограничена по цене max_domestic_leg_rub) — зависит от того, откуда
    летим: если origin_code сам в HUBS/Вьетнаме (обратный маршрут Вьетнам->
    Россия) — внутренняя нога первая (origin->hub), международная вторая
    (hub->destination); если origin_code — российский город (обычный
    маршрут в Нячанг) — наоборот, международная первая, внутренняя вторая.
    В обоих случаях именно "длинную" ногу требуем прямой — не стоит
    стыковать с ещё одной пересадкой уже пересадочный международный рейс."""
    leg_a_is_domestic = origin_code in _VIETNAM_AIRPORTS  # origin уже во Вьетнаме — первая нога внутренняя
    dom_end = end + timedelta(days=settings.max_hub_layover_days)
    try:
        leg_a_all, leg_b_all = await asyncio.gather(
            get_cheapest_in_window(tp_client, origin_code, hub_code, start, end),
            get_cheapest_in_window(tp_client, hub_code, destination_code, start, dom_end),
        )
    except Exception:
        log.exception("Ошибка поиска через хаб %s (%s -> %s)", hub_code, origin_code, destination_code)
        return []
    if not leg_a_all or not leg_b_all:
        return []

    if leg_a_is_domestic:
        leg_a_candidates = [
            c for c in leg_a_all if c["price"] <= settings.max_domestic_leg_rub and _within_transfer_limit(c)
        ][:5]
        leg_b_direct_only = [c for c in leg_b_all if (c.get("raw") or {}).get("number_of_changes") == 0]
    else:
        leg_a_candidates = [c for c in leg_a_all if (c.get("raw") or {}).get("number_of_changes") == 0][:5]
        leg_b_direct_only = None  # leg_b — внутренняя, ценовой фильтр применяем позже по max_domestic_leg_rub

    if not leg_a_candidates:
        return []
    leg_b_pool = leg_b_direct_only if leg_b_direct_only is not None else leg_b_all
    if not leg_b_pool:
        return []
    leg_b_by_date = {c["date"]: c for c in leg_b_pool}

    leg_a_dates = {c["date"] for c in leg_a_candidates}
    leg_b_dates_needed = {
        c["date"] + timedelta(days=delta)
        for c in leg_a_candidates
        for delta in range(0, settings.max_hub_layover_days + 1)
    }
    try:
        leg_a_meta, leg_b_meta = await asyncio.gather(
            _get_route_metadata(origin_code, hub_code, leg_a_dates),
            _get_route_metadata(hub_code, destination_code, leg_b_dates_needed),
        )
    except Exception:
        log.exception("Ошибка получения метаданных для хаба %s (%s -> %s)", hub_code, origin_code, destination_code)
        leg_a_meta, leg_b_meta = {}, {}

    origin_name = ORIGIN_NAMES.get(origin_code, origin_code)
    destination_name = DESTINATION_NAMES.get(destination_code, destination_code)

    for leg_a_item in leg_a_candidates:
        arrival_date = leg_a_item["date"]
        leg_a_info = leg_a_meta.get(arrival_date)
        leg_a_departure_at = leg_a_info.get("departure_at") if leg_a_info else None
        duration_min = (leg_a_item.get("raw") or {}).get("duration")
        arrival_estimate = (
            leg_a_departure_at + timedelta(minutes=duration_min)
            if leg_a_departure_at and duration_min else None
        )

        verified: list[tuple[float, dict, dict | None]] = []
        fallback: list[tuple[float, dict, dict | None]] = []
        for delta in range(0, settings.max_hub_layover_days + 1):
            cand_date = arrival_date + timedelta(days=delta)
            leg_b_item = leg_b_by_date.get(cand_date)
            if not leg_b_item:
                continue
            if leg_b_direct_only is None and (
                leg_b_item["price"] > settings.max_domestic_leg_rub or not _within_transfer_limit(leg_b_item)
            ):
                continue
            leg_b_info = leg_b_meta.get(cand_date)
            leg_b_departure_at = leg_b_info.get("departure_at") if leg_b_info else None
            if arrival_estimate and leg_b_departure_at:
                gap_hours = (leg_b_departure_at - arrival_estimate).total_seconds() / 3600
                if gap_hours >= settings.min_hub_layover_hours:
                    verified.append((leg_b_item["price"], leg_b_item, leg_b_info))
            elif delta >= settings.min_hub_layover_days:
                fallback.append((leg_b_item["price"], leg_b_item, leg_b_info))

        chosen, chosen_info, time_verified = None, None, False
        if verified:
            _, chosen, chosen_info = min(verified, key=lambda c: c[0])
            time_verified = True
        elif fallback:
            _, chosen, chosen_info = min(fallback, key=lambda c: c[0])

        if chosen is None:
            continue
        leg_a_leg = _make_leg(origin_code, hub_code, leg_a_item, leg_a_info)
        leg_b_leg = _make_leg(hub_code, destination_code, chosen, chosen_info)
        label = f"Из {origin_name} через {hub_name} ({hub_code}) в {destination_name}"
        if not time_verified:
            label += f" ⚠️ время рейсов не найдено, стыковка ≥{settings.min_hub_layover_days} сут. для подстраховки"
        return [RouteOption(
            total_price=leg_a_leg.price + leg_b_leg.price,
            legs=[leg_a_leg, leg_b_leg],
            label=label,
        )]
    return []


def _apply_priority(options: list[RouteOption]) -> None:
    """Среди вариантов, что укладываются в priority_margin_rub от самого
    дешёвого (по умолчанию 10 000 ₽/чел.), выбираем не просто дешевейший, а
    более ПРИОРИТЕТНЫЙ по двум признакам (в таком порядке): 1) прямой рейс
    (1 нога) предпочтительнее рейса через хаб (2 ноги) — реже пересадка,
    меньше риска; 2) город назначения — чем раньше в settings.destinations,
    тем приоритетнее. Экономия в пределах порога не стоит того, чтобы
    жертвовать простотой маршрута или предпочитаемым городом прилёта."""
    if not options:
        return
    cheapest_price = options[0].total_price

    def _preference(option: RouteOption) -> tuple[int, int]:
        is_via_hub = 1 if len(option.legs) > 1 else 0
        destination_code = option.legs[-1].destination
        destination_rank = (
            settings.destinations.index(destination_code)
            if destination_code in settings.destinations
            else len(settings.destinations)
        )
        return (is_via_hub, destination_rank)

    close_enough = [o for o in options if o.total_price - cheapest_price <= settings.priority_margin_rub]
    if not close_enough:
        return
    most_preferred = min(close_enough, key=_preference)
    if most_preferred is options[0]:
        return
    options.remove(most_preferred)
    options.insert(0, most_preferred)


async def gather_route_options(start: date, end: date) -> list[RouteOption]:
    """Прямые варианты для каждой пары (аэропорт вылета из settings.origins,
    город назначения из settings.destinations), плюс варианты через крупные
    вьетнамские хабы (config.HUBS), если settings.enable_hub_search включён
    (имеет смысл только когда маршрут связан с Вьетнамом) и внутренняя нога
    укладывается в max_domestic_leg_rub. Даты двух ног при варианте через
    хаб — лучшие в окне поиска по отдельности, а не обязательно
    стыкующиеся день-в-день: это ориентир, стыковку нужно проверять вручную
    по ссылкам."""
    tasks = []
    for origin_code in settings.origins:
        for destination_code in settings.destinations:
            tasks.append(_search_direct(origin_code, destination_code, start, end))
            if settings.enable_hub_search:
                for hub_code, hub_name in HUBS.items():
                    tasks.append(_search_via_hub(origin_code, hub_code, hub_name, destination_code, start, end))

    results = await asyncio.gather(*tasks)
    options: list[RouteOption] = [option for group in results for option in group]
    options.sort(key=lambda o: o.total_price)
    return options


def _format_leg_datetime(leg: Leg) -> str:
    """Дата + время вылета, как на билете (локальное время аэропорта вылета).
    Время показываем, только если реально нашли его в календаре Aviasales —
    не гадаем, когда данных нет."""
    if leg.departure_at:
        return leg.departure_at.strftime("%d.%m, вылет %H:%M")
    return leg.date.strftime("%d.%m")


def format_option(option: RouteOption) -> str:
    total_family = estimate_total_price(option.total_price)
    first_leg_dt = _format_leg_datetime(option.legs[0])
    leg_lines = [
        f"  {leg.origin}→{leg.destination}, {_format_leg_datetime(leg)}: {leg.price:.0f} ₽/чел. {_baggage_hint(leg.airline)}\n  {leg.link}"
        for leg in option.legs
    ]
    return (
        f"{first_leg_dt} — {option.label} — {option.total_price:.0f} ₽/чел. ≈ {total_family:.0f} ₽ {_total_label()}\n"
        + "\n".join(leg_lines)
    )


def format_results(options: list[RouteOption], limit: int = 5) -> str:
    return "\n\n".join(format_option(o) for o in options[:limit])


def _search_window() -> tuple[date, date]:
    """Если задан фиксированный период (конкретная поездка) — используем его,
    подрезая начало по сегодняшнему дню, чтобы не искать в уже прошедшую
    дату. Иначе — обычное скользящее окно от сегодня."""
    today = date.today()
    if settings.search_start_date and settings.search_end_date:
        return max(settings.search_start_date, today), settings.search_end_date
    return today, today + timedelta(days=settings.search_window_days)


async def run_search(chat_id: int, *, force_reply: bool) -> None:
    start, window_end = _search_window()
    context_end = window_end + timedelta(days=settings.market_context_extra_days)
    headline_info = await _cheapest_direct_headline()  # первая информация в любом сообщении, всегда

    try:
        # Забираем сразу расширенное окно (+market_context_extra_days) одним
        # запросом — оно нужно ТОЛЬКО для медианы рынка (market_stats ниже),
        # даты за пределами window_end как варианты для покупки не показываем
        # (см. фильтр options ниже) — "фоново мониторим, но не предлагаем".
        all_options = await gather_route_options(start, context_end)
    except Exception:
        log.exception("Ошибка запроса к Travelpayouts для чата %s", chat_id)
        if force_reply:
            await bot.send_message(chat_id, headline_info + "Не удалось получить данные от Travelpayouts, попробуй позже.")
        return

    median_price, sample_size = market_stats(all_options)
    options = [o for o in all_options if o.legs[0].date <= window_end]
    options.sort(key=lambda o: o.total_price)
    _apply_priority(options)

    if not options:
        if force_reply:
            fallback_link = build_booking_link(
                settings.origins[0], settings.destinations[0], start,
                settings.adults, settings.children, settings.infants, settings.travelpayouts_marker,
            )
            hub_note = " (включая варианты через хабы)" if settings.enable_hub_search else ""
            await bot.send_message(
                chat_id,
                headline_info + f"По этому направлению{hub_note} пока нет данных в кэше Aviasales. "
                f"Проверь вручную:\n{fallback_link}",
            )
        return

    best = options[0]
    total_estimate = estimate_total_price(best.total_price)
    budget = budget_for_chat(chat_id)

    def _deal_tags(option: RouteOption) -> list[str]:
        tags = []
        if option.total_price <= budget:
            tags.append(f"🔥 ниже {budget:.0f} ₽/чел.")
        if sample_size >= 3 and option.total_price <= median_price * MARKET_BEATING_RATIO:
            pct = (1 - option.total_price / median_price) * 100
            tags.append(f"🎯 на {pct:.0f}% дешевле рынка (медиана ~{median_price:.0f} ₽/чел.)")
        return tags

    overall_tags = _deal_tags(best)

    # Самый приоритетный город назначения (settings.destinations[0]) может не
    # попасть в топ-5 по цене, если он ощутимо (больше priority_margin_rub)
    # дороже остальных — показываем его отдельным блоком, чтобы не терялся.
    top_priority_destination = settings.destinations[0]
    priority_options = [o for o in options if o.legs[-1].destination == top_priority_destination]
    best_priority = priority_options[0] if priority_options else None
    priority_total_estimate = estimate_total_price(best_priority.total_price) if best_priority else None
    priority_tags = _deal_tags(best_priority) if best_priority else []

    top_shown = options[:5]
    body = headline_info + (
        f"{'/'.join(settings.origins)} → {'/'.join(settings.destinations)}, в одну сторону:\n\n{format_results(options)}"
    )
    if best_priority and best_priority not in top_shown:
        priority_name = DESTINATION_NAMES.get(top_priority_destination, top_priority_destination)
        body += f"\n\nОтдельно в {priority_name} (приоритетный город прилёта):\n{format_option(best_priority)}"

    now_iso = datetime.now(timezone.utc).isoformat()

    if force_reply:
        await bot.send_message(chat_id, body)
        store.update_alert_state(chat_id, total_estimate, now_iso)
        if best_priority:
            store.update_alert_state(chat_id, priority_total_estimate, now_iso, prefix="priority_alert")
        return

    # Плановая проверка шлёт сообщение КАЖДЫЙ РАЗ, не только когда выгодно —
    # так видно динамику цен и можно понять, что на этом маршруте вообще
    # считается средней ценой, а не только моменты явных распродаж (🔥/🎯).
    chat = store.get_chat(chat_id)
    last_price = chat.get("last_alert_price")
    if last_price is None:
        headline = "✈️ Первая проверка — вот что нашёл:"
    elif total_estimate <= last_price * ALERT_MEANINGFUL_DROP:
        headline = f"📉 Цена упала! Было {last_price:.0f} ₽, стало {total_estimate:.0f} ₽ (на всех):"
    else:
        headline = "📊 Текущие цены:"
    if overall_tags:
        headline += "\n" + "\n".join(overall_tags)
    if priority_tags:
        priority_name = DESTINATION_NAMES.get(top_priority_destination, top_priority_destination)
        headline += f"\nВ {priority_name}: " + ", ".join(priority_tags)

    await bot.send_message(chat_id, f"{headline}\n\n{body}")
    store.update_alert_state(chat_id, total_estimate, now_iso)
    if best_priority:
        store.update_alert_state(chat_id, priority_total_estimate, now_iso, prefix="priority_alert")


def _window_description() -> str:
    if settings.search_start_date and settings.search_end_date:
        return f"с {settings.search_start_date:%d.%m} по {settings.search_end_date:%d.%m}"
    return f"на ближайшие {settings.search_window_days} дней"


def _plural_ru(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return few
    return many


def _passengers_description() -> str:
    """Все пассажиры считаются взрослыми (см. Settings.adults/children) —
    описание собирается динамически, а не зашито под конкретное число детей."""
    parts = [f"{settings.adults} {_plural_ru(settings.adults, 'взрослый', 'взрослых', 'взрослых')}"]
    if settings.children:
        parts.append(f"{settings.children} {_plural_ru(settings.children, 'ребёнок', 'ребёнка', 'детей')}")
    if settings.infants:
        parts.append(f"{settings.infants} {_plural_ru(settings.infants, 'младенец', 'младенца', 'младенцев')}")
    return " + ".join(parts)


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    store.subscribe(message.chat.id)
    budget = budget_for_chat(message.chat.id)
    origins_names = " и ".join(ORIGIN_NAMES.get(o, o) for o in settings.origins)
    destinations_names = " > ".join(DESTINATION_NAMES.get(d, d) for d in settings.destinations)
    hub_line = (
        f"{_window_description()} — включая варианты через хабы "
        f"({', '.join(HUBS.values())}): прямой рейс показываю впереди рейса через хаб, только если "
        f"экономия от пересадки не больше {settings.priority_margin_rub:.0f} ₽/чел., иначе показываю "
        "маршрут через хаб.\n"
        if settings.enable_hub_search
        else f"{_window_description()}.\n"
    )
    destinations_line = (
        f"Города прилёта — по приоритету: {destinations_names}. Если разница между ними не больше "
        f"{settings.priority_margin_rub:.0f} ₽/чел. — предпочитаю более приоритетный, а не просто самый "
        "дешёвый; если приоритетный ощутимо дороже, отмечаю его отдельной строкой, даже если он не "
        "попал в топ по цене.\n"
        if len(settings.destinations) > 1
        else ""
    )
    await message.answer(
        "Привет! Слежу за билетами "
        f"{'/'.join(settings.origins)} → {'/'.join(settings.destinations)} (вылет из {origins_names}), "
        f"в одну сторону, "
        f"{_passengers_description()}, "
        f"{hub_line}"
        f"Проверяю раз в {settings.check_interval_minutes} мин. и пишу тебе САМ каждый раз — не нужно "
        "спрашивать вручную. Так видно, как вообще меняются цены на маршруте, а не только моменты "
        "явных распродаж — те дополнительно помечаю значками 🔥 (ниже твоего порога) и 🎯 (заметно "
        "дешевле текущей медианы по рынку).\n"
        f"{destinations_line}"
        f"Порог для отметки 🔥: {budget} ₽/чел. (можно менять через /setbudget).\n\n"
        "Команды:\n"
        "/check — проверить цены прямо сейчас\n"
        "/setbudget 35000 — изменить порог отметки 🔥 (в рублях за человека)\n"
        "/status — текущие настройки\n"
        "/stop — отписаться от уведомлений"
    )


@dp.message(Command("stop"))
async def cmd_stop(message: Message) -> None:
    store.unsubscribe(message.chat.id)
    await message.answer("Отписал от уведомлений. Чтобы включить снова — /start")


@dp.message(Command("setbudget"))
async def cmd_setbudget(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Использование: /setbudget 30000 (порог для отметки 🔥, в рублях ЗА ЧЕЛОВЕКА)")
        return
    budget = int(arg)
    store.set_budget(message.chat.id, budget)
    await message.answer(
        f"Порог обновлён: {budget} ₽/чел. Учти: сигналы о новой лучшей цене приходят "
        "в любом случае, порог только добавляет отметку 🔥, когда цена ещё и ниже него."
    )


@dp.message(Command("status"))
async def cmd_status(message: Message) -> None:
    budget = budget_for_chat(message.chat.id)
    origins_names = " и ".join(ORIGIN_NAMES.get(o, o) for o in settings.origins)
    destinations_names = " > ".join(DESTINATION_NAMES.get(d, d) for d in settings.destinations)
    hub_lines = (
        f"Плюс варианты через хабы: {', '.join(f'{name} ({code})' for code, name in HUBS.items())} "
        f"— если внутренняя (по Вьетнаму) нога маршрута дешевле {settings.max_domestic_leg_rub} ₽\n"
        f"Приоритет прямого рейса: хаб-маршрут показываю впереди прямого, только если экономит больше "
        f"{settings.priority_margin_rub:.0f} ₽/чел. — иначе показываю прямой\n"
        f"Запас на стыковку через хаб: ≥{settings.min_hub_layover_hours}ч, если время рейсов нашлось "
        f"в календаре Aviasales; если нет — подстраховка минимум {settings.min_hub_layover_days} сутки "
        "(с пометкой ⚠️ в названии варианта)\n"
        if settings.enable_hub_search
        else "Поиск через хабы: выключен (ENABLE_HUB_SEARCH=false)\n"
    )
    destinations_line = (
        f"Города прилёта по приоритету: {destinations_names} — разница до "
        f"{settings.priority_margin_rub:.0f} ₽/чел. не в счёт, предпочитаю более приоритетный\n"
        if len(settings.destinations) > 1
        else ""
    )
    await message.answer(
        f"Маршрут: {'/'.join(settings.origins)} → {'/'.join(settings.destinations)} (вылет из {origins_names}), "
        "в одну сторону\n"
        f"{destinations_line}"
        f"{hub_lines}"
        f"Багаж: 🧳✅/❌/❓ по типу перевозчика (не по тарифу — сверяй при бронировании)\n"
        f"Окно поиска: {_window_description()}\n"
        f"Пассажиры: {_passengers_description()} (все считаются взрослыми — детский "
        "тариф обычно действует примерно до 12 лет)\n"
        "Сигналы: пишу сам, БЕЗ запроса, на каждой проверке — чтобы было видно динамику цен, "
        "а не только моменты явных распродаж (те дополнительно отмечены 🔥/🎯)\n"
        f"Порог для отметки 🔥: {budget} ₽/чел.\n"
        f"Отметка 🎯: цена ≤{MARKET_BEATING_RATIO*100:.0f}% от медианы по текущей выдаче — "
        "«заметно дешевле рынка», медиана считается заново на каждой проверке\n"
        + (f"Приоритетный город прилёта ({DESTINATION_NAMES.get(settings.destinations[0], settings.destinations[0])}) "
           "отслеживается отдельным треком — выгодный вариант по нему пришлю, даже если общий лучший "
           "вариант сейчас в другой город\n" if len(settings.destinations) > 1 else "")
        + f"Периодичность проверки: раз в {settings.check_interval_minutes} мин."
    )


@dp.message(Command("check"))
async def cmd_check(message: Message) -> None:
    store.subscribe(message.chat.id)
    await message.answer("Ищу цены, секунду…")
    await run_search(message.chat.id, force_reply=True)


async def scheduled_job() -> None:
    for chat_id in store.all_chat_ids():
        try:
            await run_search(chat_id, force_reply=False)
        except Exception:
            log.exception("Ошибка планового поиска для чата %s", chat_id)


async def main() -> None:
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    # next_run_time тут не используем: naive datetime.now() планировщик интерпретирует
    # как локальное время В ЧАСОВОМ ПОЯСЕ Europe/Moscow, а не как текущий момент —
    # если сервер не в MSK, "немедленный" запуск на деле откладывается на разницу
    # поясов. Поэтому первую проверку просто вызываем явно, до старта интервала.
    scheduler.add_job(scheduled_job, "interval", minutes=settings.check_interval_minutes)
    scheduler.start()
    log.info(
        "Бот запущен: %s -> %s, окно %s, проверка каждые %s мин.",
        "/".join(settings.origins), "/".join(settings.destinations), _window_description(), settings.check_interval_minutes,
    )
    await scheduled_job()
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await tp_client.aclose()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
