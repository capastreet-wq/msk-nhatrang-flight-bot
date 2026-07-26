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

from config import AIRLINE_BAGGAGE_INCLUDED, HUBS, ORIGIN_NAMES, Settings, load_settings
from storage import Store
from travelpayouts import TravelpayoutsClient, build_booking_link, get_cheapest_in_window, months_between

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("flight-bot")

settings: Settings = load_settings()
store = Store(settings.data_dir / "state.json")
bot = Bot(token=settings.telegram_token, default=DefaultBotProperties(parse_mode=None))
dp = Dispatcher()
tp_client = TravelpayoutsClient(settings.travelpayouts_token, settings.currency)

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


async def _search_direct(origin_code: str, start: date, end: date) -> list[RouteOption]:
    try:
        direct = await get_cheapest_in_window(tp_client, origin_code, settings.destination, start, end)
    except Exception:
        log.exception("Ошибка поиска прямого варианта %s -> %s", origin_code, settings.destination)
        return []
    candidates = direct[:3]
    try:
        meta_by_date = await _get_route_metadata(origin_code, settings.destination, {c["date"] for c in candidates})
    except Exception:
        log.exception("Ошибка получения метаданных %s -> %s", origin_code, settings.destination)
        meta_by_date = {}
    origin_name = ORIGIN_NAMES.get(origin_code, origin_code)
    options = []
    for item in candidates:
        leg = _make_leg(origin_code, settings.destination, item, meta_by_date.get(item["date"]))
        options.append(RouteOption(total_price=leg.price, legs=[leg], label=f"Прямой из {origin_name}"))
    return options


async def _cheapest_direct_headline() -> str:
    """Первая строка каждого сообщения: самый дешёвый прямой билет (один
    билет, без сборки через хаб — то же значение "прямой", что и у остальных
    RouteOption с label "Прямой из ...") по первому аэропорту вылета
    (settings.origins[0]) на ближайшие 3 дня. Отдельный, более узкий поиск
    от основного окна (settings.search_start_date/end) — под наблюдение,
    что горящие предложения появляются за пару-тройку дней до вылета."""
    origin = settings.origins[0]
    today = date.today()
    near_end = today + timedelta(days=3)
    try:
        candidates = await get_cheapest_in_window(tp_client, origin, settings.destination, today, near_end)
    except Exception:
        log.exception("Ошибка поиска ближайшего прямого рейса %s->%s", origin, settings.destination)
        return f"✈️ Не удалось проверить ближайшие прямые рейсы {origin}→{settings.destination} (ошибка API)\n\n"
    if not candidates:
        return f"✈️ На ближайшие 3 дня прямых билетов {origin}→{settings.destination} не найдено в кэше Aviasales\n\n"
    best = min(candidates, key=lambda c: c["price"])
    try:
        meta = await _get_route_metadata(origin, settings.destination, {best["date"]})
    except Exception:
        log.exception("Ошибка получения метаданных для ближайшего прямого рейса %s->%s", origin, settings.destination)
        meta = {}
    leg = _make_leg(origin, settings.destination, best, meta.get(best["date"]))
    total_family = estimate_total_price(leg.price)
    return (
        f"✈️ Самый дешёвый прямой {origin}→{settings.destination} на ближайшие 3 дня: "
        f"{_format_leg_datetime(leg)} — {leg.price:.0f} ₽/чел. ≈ {total_family:.0f} ₽ {_total_label()} {_baggage_hint(leg.airline)}\n"
        f"{leg.link}\n\n"
    )


async def _search_via_hub(origin_code: str, hub_code: str, hub_name: str, start: date, end: date) -> list[RouteOption]:
    """Подбирает пару билетов origin->hub и hub->CXR с реальным запасом на
    пересадку. v1/prices/calendar отдаёт время вылета (departure_at) для
    большинства маршрутов — используем его вместе с длительностью рейса
    (duration из v2/prices/latest), чтобы оценить время прилёта в хаб и
    сравнить с временем вылета внутреннего рейса. Если для какой-то даты
    время не нашлось — используем запасной вариант: минимум
    min_hub_layover_days суток, с явной пометкой в названии варианта, что
    время не подтверждено.

    Ногу origin->hub учитываем, ТОЛЬКО если это прямой рейс (number_of_changes
    == 0 в сырых данных v2/prices/latest) — иначе это уже две пересадки на
    пути в Нячанг (по пути к хабу + сам хаб->CXR), а не одна, и оправдать
    это сложнее даже при экономии в $100+."""
    dom_end = end + timedelta(days=settings.max_hub_layover_days)
    try:
        intl, dom = await asyncio.gather(
            get_cheapest_in_window(tp_client, origin_code, hub_code, start, end),
            get_cheapest_in_window(tp_client, hub_code, settings.destination, start, dom_end),
        )
    except Exception:
        log.exception("Ошибка поиска через хаб %s из %s", hub_code, origin_code)
        return []
    if not intl or not dom:
        return []

    intl_candidates = [c for c in intl if (c.get("raw") or {}).get("number_of_changes") == 0][:5]
    if not intl_candidates:
        return []
    intl_dates = {c["date"] for c in intl_candidates}
    dom_dates_needed = {
        c["date"] + timedelta(days=delta)
        for c in intl_candidates
        for delta in range(0, settings.max_hub_layover_days + 1)
    }
    try:
        intl_meta, dom_meta = await asyncio.gather(
            _get_route_metadata(origin_code, hub_code, intl_dates),
            _get_route_metadata(hub_code, settings.destination, dom_dates_needed),
        )
    except Exception:
        log.exception("Ошибка получения метаданных для хаба %s из %s", hub_code, origin_code)
        intl_meta, dom_meta = {}, {}

    dom_by_date = {d["date"]: d for d in dom}
    origin_name = ORIGIN_NAMES.get(origin_code, origin_code)

    for intl_item in intl_candidates:
        arrival_date = intl_item["date"]
        intl_info = intl_meta.get(arrival_date)
        intl_departure_at = intl_info.get("departure_at") if intl_info else None
        duration_min = (intl_item.get("raw") or {}).get("duration")
        arrival_estimate = (
            intl_departure_at + timedelta(minutes=duration_min)
            if intl_departure_at and duration_min else None
        )

        verified: list[tuple[float, dict, dict | None]] = []
        fallback: list[tuple[float, dict, dict | None]] = []
        for delta in range(0, settings.max_hub_layover_days + 1):
            cand_date = arrival_date + timedelta(days=delta)
            dom_item = dom_by_date.get(cand_date)
            if not dom_item or dom_item["price"] > settings.max_domestic_leg_rub:
                continue
            dom_info = dom_meta.get(cand_date)
            dom_departure_at = dom_info.get("departure_at") if dom_info else None
            if arrival_estimate and dom_departure_at:
                gap_hours = (dom_departure_at - arrival_estimate).total_seconds() / 3600
                if gap_hours >= settings.min_hub_layover_hours:
                    verified.append((dom_item["price"], dom_item, dom_info))
            elif delta >= settings.min_hub_layover_days:
                fallback.append((dom_item["price"], dom_item, dom_info))

        chosen, chosen_info, time_verified = None, None, False
        if verified:
            _, chosen, chosen_info = min(verified, key=lambda c: c[0])
            time_verified = True
        elif fallback:
            _, chosen, chosen_info = min(fallback, key=lambda c: c[0])

        if chosen is None:
            continue
        intl_leg = _make_leg(origin_code, hub_code, intl_item, intl_info)
        dom_leg = _make_leg(hub_code, settings.destination, chosen, chosen_info)
        label = f"Из {origin_name} через {hub_name} ({hub_code})"
        if not time_verified:
            label += f" ⚠️ время рейсов не найдено, стыковка ≥{settings.min_hub_layover_days} сут. для подстраховки"
        return [RouteOption(
            total_price=intl_leg.price + dom_leg.price,
            legs=[intl_leg, dom_leg],
            label=label,
        )]
    return []


def _apply_direct_priority(options: list[RouteOption]) -> None:
    """Прямой перелёт (1 нога, сразу до Нячанга) ставим первым, если он
    дороже самого дешёвого варианта не больше чем на hub_min_savings_rub
    (по умолчанию $100/чел. по курсу settings.usd_rub_rate) — пересадка
    через хаб того стоит, только если реально экономит эту сумму на
    человека, иначе не городим лишний билет и риск стыковки ради мелочи."""
    if not options:
        return
    cheapest_price = options[0].total_price
    direct_options = [o for o in options if len(o.legs) == 1]
    if not direct_options:
        return
    best_direct = min(direct_options, key=lambda o: o.total_price)
    if best_direct is options[0]:
        return
    if best_direct.total_price - cheapest_price <= settings.hub_min_savings_rub:
        options.remove(best_direct)
        options.insert(0, best_direct)


async def gather_route_options(start: date, end: date) -> list[RouteOption]:
    """Прямые варианты для каждого аэропорта вылета (settings.origins), плюс
    варианты через крупные вьетнамские хабы (config.HUBS), если
    settings.enable_hub_search включён (имеет смысл только когда
    destination — Нячанг/Вьетнам) и внутренний перелёт хаб->CXR укладывается
    в max_domestic_leg_rub. Даты двух ног при варианте через хаб — лучшие в
    окне поиска по отдельности, а не обязательно стыкующиеся день-в-день:
    это ориентир, стыковку нужно проверять вручную по ссылкам."""
    tasks = []
    for origin_code in settings.origins:
        tasks.append(_search_direct(origin_code, start, end))
        if settings.enable_hub_search:
            for hub_code, hub_name in HUBS.items():
                tasks.append(_search_via_hub(origin_code, hub_code, hub_name, start, end))

    results = await asyncio.gather(*tasks)
    options: list[RouteOption] = [option for group in results for option in group]
    options.sort(key=lambda o: o.total_price)
    _apply_direct_priority(options)
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
    headline_info = await _cheapest_direct_headline()  # первая информация в любом сообщении, всегда

    try:
        options = await gather_route_options(start, window_end)
    except Exception:
        log.exception("Ошибка запроса к Travelpayouts для чата %s", chat_id)
        if force_reply:
            await bot.send_message(chat_id, headline_info + "Не удалось получить данные от Travelpayouts, попробуй позже.")
        return

    if not options:
        if force_reply:
            fallback_link = build_booking_link(
                settings.origins[0], settings.destination, start,
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
    median_price, sample_size = market_stats(options)

    def _deal_tags(option: RouteOption) -> list[str]:
        tags = []
        if option.total_price <= budget:
            tags.append(f"🔥 ниже {budget:.0f} ₽/чел.")
        if sample_size >= 3 and option.total_price <= median_price * MARKET_BEATING_RATIO:
            pct = (1 - option.total_price / median_price) * 100
            tags.append(f"🎯 на {pct:.0f}% дешевле рынка (медиана ~{median_price:.0f} ₽/чел.)")
        return tags

    overall_tags = _deal_tags(best)

    mrv_options = [o for o in options if o.legs[0].origin == "MRV"]
    best_mrv = mrv_options[0] if mrv_options else None
    mrv_total_estimate = estimate_total_price(best_mrv.total_price) if best_mrv else None
    mrv_tags = _deal_tags(best_mrv) if best_mrv else []

    top_shown = options[:5]
    body = headline_info + f"{'/'.join(settings.origins)} → {settings.destination}, в одну сторону:\n\n{format_results(options)}"
    if best_mrv and best_mrv not in top_shown:
        body += f"\n\nОтдельно из Минеральных Вод:\n{format_option(best_mrv)}"

    now_iso = datetime.now(timezone.utc).isoformat()

    if force_reply:
        await bot.send_message(chat_id, body)
        store.update_alert_state(chat_id, total_estimate, now_iso)
        if best_mrv:
            store.update_alert_state(chat_id, mrv_total_estimate, now_iso, prefix="mrv_alert")
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
    if mrv_tags:
        headline += "\nИз Минеральных Вод: " + ", ".join(mrv_tags)

    await bot.send_message(chat_id, f"{headline}\n\n{body}")
    store.update_alert_state(chat_id, total_estimate, now_iso)
    if best_mrv:
        store.update_alert_state(chat_id, mrv_total_estimate, now_iso, prefix="mrv_alert")


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
    hub_line = (
        f"{_window_description()} — включая варианты через хабы "
        f"({', '.join(HUBS.values())}): пересадку через хаб показываю впереди прямого, только если "
        f"она реально экономит от ${settings.hub_min_savings_usd:.0f}/чел. (~{settings.hub_min_savings_rub:.0f} ₽ "
        f"по курсу {settings.usd_rub_rate:.0f} ₽/$), иначе показываю прямой рейс.\n"
        if settings.enable_hub_search
        else f"{_window_description()}.\n"
    )
    mrv_line = (
        "Отдельно слежу за Минеральными Водами — если по ним появляется что-то стоящее, отмечаю "
        "отдельной строкой, даже если общий лучший вариант сейчас из Москвы.\n"
        if "MRV" in settings.origins and len(settings.origins) > 1
        else ""
    )
    await message.answer(
        "Привет! Слежу за билетами "
        f"{'/'.join(settings.origins)} → {settings.destination} (вылет из {origins_names}, "
        f"конечная точка всегда {settings.destination}), в одну сторону, "
        f"{_passengers_description()}, "
        f"{hub_line}"
        f"Проверяю раз в {settings.check_interval_minutes} мин. и пишу тебе САМ каждый раз — не нужно "
        "спрашивать вручную. Так видно, как вообще меняются цены на маршруте, а не только моменты "
        "явных распродаж — те дополнительно помечаю значками 🔥 (ниже твоего порога) и 🎯 (заметно "
        "дешевле текущей медианы по рынку).\n"
        f"{mrv_line}"
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
    hub_lines = (
        f"Плюс варианты через хабы: {', '.join(f'{name} ({code})' for code, name in HUBS.items())} "
        f"— если внутренний перелёт до {settings.destination} дешевле {settings.max_domestic_leg_rub} ₽\n"
        f"Приоритет прямого рейса: хаб-маршрут показываю впереди прямого, только если экономит от "
        f"${settings.hub_min_savings_usd:.0f}/чел. (~{settings.hub_min_savings_rub:.0f} ₽ по курсу "
        f"{settings.usd_rub_rate:.0f} ₽/$) — иначе показываю прямой\n"
        f"Запас на стыковку через хаб: ≥{settings.min_hub_layover_hours}ч, если время рейсов нашлось "
        f"в календаре Aviasales; если нет — подстраховка минимум {settings.min_hub_layover_days} сутки "
        "(с пометкой ⚠️ в названии варианта)\n"
        if settings.enable_hub_search
        else "Поиск через хабы: выключен (ENABLE_HUB_SEARCH=false)\n"
    )
    await message.answer(
        f"Маршрут: {'/'.join(settings.origins)} → {settings.destination} (вылет из {origins_names}), "
        f"в одну сторону, конечная точка всегда {settings.destination}\n"
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
        + ("Минеральные Воды отслеживаются отдельным треком — выгодный вариант оттуда пришлю, "
           "даже если общий лучший вариант сейчас из Москвы\n" if "MRV" in settings.origins and len(settings.origins) > 1 else "")
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
        "/".join(settings.origins), settings.destination, _window_description(), settings.check_interval_minutes,
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
