"""Простое персистентное хранилище на JSON-файле — для личного бота на 1-5 чатов
полноценная БД избыточна, а состояние (подписчики, пороги, история алертов)
должно переживать перезапуск процесса.
"""
import json
import threading
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {"chats": {}}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def subscribe(self, chat_id: int) -> None:
        with self._lock:
            self._data["chats"].setdefault(
                str(chat_id),
                {"budget": None, "last_alert_price": None, "last_alert_at": None},
            )
            self._save()

    def unsubscribe(self, chat_id: int) -> None:
        with self._lock:
            self._data["chats"].pop(str(chat_id), None)
            self._save()

    def all_chat_ids(self) -> list[int]:
        with self._lock:
            return [int(cid) for cid in self._data["chats"].keys()]

    def get_chat(self, chat_id: int) -> dict[str, Any]:
        with self._lock:
            return dict(self._data["chats"].get(str(chat_id), {}))

    def set_budget(self, chat_id: int, budget: int) -> None:
        with self._lock:
            self._data["chats"].setdefault(str(chat_id), {})["budget"] = budget
            self._save()

    def update_alert_state(self, chat_id: int, price: float, when_iso: str, prefix: str = "last_alert") -> None:
        """prefix позволяет вести независимые треки цен — например,
        'last_alert' для лучшей цены в целом и 'priority_alert' отдельно
        для лучшей цены в самом приоритетном городе назначения, чтобы
        выгодный вариант оттуда не терялся на фоне более дешёвых
        альтернатив в других городах DESTINATIONS."""
        with self._lock:
            chat = self._data["chats"].setdefault(str(chat_id), {})
            chat[f"{prefix}_price"] = price
            chat[f"{prefix}_at"] = when_iso
            self._save()
