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
            self._data["chats"].setdefault(str(chat_id), {"budget": None})
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

    def set_field(self, chat_id: int, key: str, value: Any) -> None:
        """Универсальное хранение произвольных полей состояния по чату —
        используется для трека алертов о дешёвых билетах, накопленного
        минимума за период дайджеста и т.п. (см. bot.py)."""
        with self._lock:
            chat = self._data["chats"].setdefault(str(chat_id), {})
            chat[key] = value
            self._save()

    def get_field(self, chat_id: int, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data["chats"].get(str(chat_id), {}).get(key, default)
