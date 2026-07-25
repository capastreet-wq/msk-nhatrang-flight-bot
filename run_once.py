"""Разовый запуск для сред без постоянно работающего процесса (GitHub Actions
по расписанию): обрабатывает накопившиеся команды в Telegram (через getUpdates
с сохранённым offset — НЕ через долгий polling) и прогоняет плановую проверку
цен для всех подписчиков. Переиспользует dp/bot/scheduled_job из bot.py — те же
обработчики команд, что и в постоянном режиме, просто без Dispatcher.start_polling().

Из-за того, что процесс не висит постоянно, команды типа /check или /setbudget
обрабатываются с задержкой до следующего запуска по расписанию (а не мгновенно).
"""
import asyncio

from bot import bot, dp, log, scheduled_job, settings, tp_client

OFFSET_FILE = settings.data_dir / "update_offset.txt"


async def process_pending_updates() -> None:
    offset = None
    if OFFSET_FILE.exists():
        raw = OFFSET_FILE.read_text().strip()
        offset = int(raw) if raw else None

    updates = await bot.get_updates(offset=offset, timeout=3, limit=100)
    for update in updates:
        try:
            await dp.feed_update(bot, update)
        except Exception:
            log.exception("Ошибка обработки апдейта %s", update.update_id)
        offset = update.update_id + 1

    if offset is not None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        OFFSET_FILE.write_text(str(offset))


async def main() -> None:
    await process_pending_updates()
    await scheduled_job()
    await tp_client.aclose()
    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
