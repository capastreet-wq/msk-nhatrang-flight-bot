"""Клиент для бесплатного Travelpayouts Data API (v2/prices/latest).

Важно: это НЕ тот API, который отдаёт живые цены с точным числом пассажиров —
это кэш минимальных цен, которые Aviasales уже видел по маршруту (обычно за
1 взрослого). Настоящий live-поиск с пассажирами (v1/flight_search) закрыт —
Travelpayouts выдаёт к нему доступ только партнёрам с MAU 50 000+ и явно
запрещает использовать его для автоматического мониторинга (см. README).

Почему v2/prices/latest, а не v1/prices/calendar: календарный эндпоинт для
этого маршрута (MOW-NHA) игнорирует параметр one_way и всегда отдаёт кэш
цен туда-обратно (проверено эмпирически — поле return_at всегда заполнено
реальной датой, независимо от one_way=true/false). v2/prices/latest с
one_way=true честно отдаёт только настоящие one-way записи (return_date
пустой) — это подтверждено на реальных данных.

Поэтому бот берёт ориентировочную one-way цену за взрослого из Data API,
оценивает стоимость на всю компанию (adults+children billed as adults) и
даёт ссылку на aviasales, где перед покупкой видна точная цена на всех
пассажиров.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any
import httpx

LATEST_URL = "https://api.travelpayouts.com/v2/prices/latest"
CALENDAR_URL = "https://api.travelpayouts.com/v1/prices/calendar"
BOOKING_BASE_URL = "https://www.aviasales.ru/search/"


class TravelpayoutsClient:
    def __init__(self, token: str, currency: str = "rub", timeout: float = 20.0):
        self.token = token
        self.currency = currency
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_latest_one_way_prices(self, origin: str, destination: str, month: str) -> list[dict]:
        """month в формате 'YYYY-MM'. Возвращает только настоящие one-way записи
        (return_date у Travelpayouts для них пустой) — см. докстринг модуля."""
        params = {
            "origin": origin,
            "destination": destination,
            "currency": self.currency,
            "period_type": "month",
            "beginning_of_period": f"{month}-01",
            "one_way": "true",
            "sorting": "price",
            "limit": 200,
            "token": self.token,
        }
        resp = await self._client.get(LATEST_URL, params=params)
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data") if isinstance(payload, dict) else payload
        return data if isinstance(data, list) else []

    async def get_calendar_metadata(self, origin: str, destination: str, month: str) -> dict[str, dict]:
        """Только время вылета и авиакомпания по датам (v1/prices/calendar) —
        НЕ цена: для некоторых маршрутов (например MOW-CXR) этот эндпоинт
        путает one-way и round-trip по цене (см. докстринг модуля), но
        departure_at/airline для конкретной даты по-прежнему отражают
        реальное расписание рейса и полезны для оценки стыковки по времени
        и подсказки про багаж. Возвращает {'YYYY-MM-DD': {'departure_at':
        datetime|None, 'airline': str|None}}."""
        params = {
            "origin": origin,
            "destination": destination,
            "depart_date": month,
            "calendar_type": "departure_date",
            "currency": self.currency,
            "token": self.token,
        }
        try:
            resp = await self._client.get(CALENDAR_URL, params=params)
            resp.raise_for_status()
        except httpx.HTTPStatusError:
            return {}
        payload = resp.json()
        data = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(data, dict):
            return {}
        result: dict[str, dict] = {}
        for date_str, info in data.items():
            if not isinstance(info, dict):
                continue
            departure_at = None
            raw_dep = info.get("departure_at")
            if raw_dep:
                try:
                    departure_at = datetime.fromisoformat(raw_dep)
                except ValueError:
                    departure_at = None
            result[date_str[:10]] = {"departure_at": departure_at, "airline": info.get("airline")}
        return result


def months_between(start: date, end: date) -> list[str]:
    months = []
    cur = date(start.year, start.month, 1)
    while cur <= end:
        months.append(f"{cur.year:04d}-{cur.month:02d}")
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)
    return months


async def get_cheapest_in_window(
    client: TravelpayoutsClient,
    origin: str,
    destination: str,
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    """Собирает one-way цены по дням в диапазоне [start, end], отсортированные
    по цене (дешевле -> дороже). Пропускает записи, которые оказались с
    заполненным return_date — Data API иногда всё равно подмешивает round-trip
    даже при one_way=true, поэтому фильтруем дополнительно на нашей стороне."""
    result: list[dict[str, Any]] = []
    for month in months_between(start, end):
        try:
            items = await client.get_latest_one_way_prices(origin, destination, month)
        except httpx.HTTPStatusError:
            items = []
        for item in items:
            if item.get("return_date"):
                continue
            depart_date = item.get("depart_date")
            price = item.get("value") or item.get("price")
            if not (depart_date and price):
                continue
            try:
                d = date.fromisoformat(depart_date[:10])
            except ValueError:
                continue
            if start <= d <= end:
                result.append({"date": d, "price": float(price), "raw": item})

    result.sort(key=lambda x: x["price"])
    return result


def build_booking_link(
    origin: str,
    destination: str,
    depart_date: date,
    adults: int,
    children: int,
    infants: int,
    marker: str | None = None,
    locale: str = "ru",
) -> str:
    """Ссылка на реальный поиск на aviasales.ru с точным числом пассажиров —
    там будет настоящая цена на всех, в отличие от кэша Data API выше.

    Формат query-параметров (origin_iata=...&destination_iata=...) на
    search.aviasales.com/flights — ПРОВЕРЕНО СЛОМАН: реальный HTTP-запрос
    получает 302-редирект на голую главную страницу без единого параметра
    (проверено curl'ом с разными User-Agent, эмуляцией браузера и т.п.).
    Вместо него используется компактный путь aviasales.ru/search/<код> —
    это подтверждённо рабочий и официальный формат для шаринг-ссылок
    (у него есть свой og:description на сайте вида "MOW (11.08) - CXR,
    1 пассажир, эконом"), запросы к нему отдают чистые 200 без редиректов.

    Кодировка пассажиров в этом коде — {adults}{children}{infants} слитно,
    отбрасывая замыкающие нули (пример с сайта: код "...1" = 1 взрослый,
    0 детей, эконом). Для 1 взрослого без детей это подтверждено; для
    1 взрослого + 2 детей (наш случай) — лучшая реконструкция по
    документированному паттерну, но её стоит один раз перепроверить
    глазами: открыть ссылку и убедиться, что на aviasales подставилось
    именно 1+2, а не что-то другое."""
    date_part = depart_date.strftime("%d%m")
    passengers = f"{adults}{children}{infants}".rstrip("0")
    link = f"{BOOKING_BASE_URL}{origin}{date_part}{destination}{passengers}"
    if marker:
        link += f"?marker={marker}"
    return link
