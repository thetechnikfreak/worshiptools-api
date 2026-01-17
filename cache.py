import yaml
from typing import Any, Optional


class YamlDatabase:
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.data = self._load()

    def _load(self) -> dict:
        try:
            with open(self.filepath, "r", encoding="utf-8") as file:
                return yaml.safe_load(file) or {}
        except FileNotFoundError:
            return {}

    def _save(self):
        with open(self.filepath, "w", encoding="utf-8") as file:
            yaml.dump(self.data, file, allow_unicode=True)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any):
        self.data[key] = value
        self._save()


class Cacher:
    def __init__(self, database: YamlDatabase):
        self.db = database

    def get(self, key: str, default: Any = None) -> Any:
        return self.db.get(key, default)

    def set(self, key: str, value: Any):
        self.db.set(key, value)
