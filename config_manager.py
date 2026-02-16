import json
from pathlib import Path
from typing import Any, Dict


class ConfigManager:
    def __init__(self, filename: str) -> None:
        self.path = Path(filename)

    def _default(self) -> Dict[str, Any]:
        return {
            "base_url": "http://localhost/alpinn.ch_dynamic/public",
            "api_key": "",
            "channels": {},
            "auto_enabled_endpoints": [],
            "auto_messages": {},
            "auto_signatures": {},
            "auto_news_messages": {},
            "auto_news_signatures": {},
            "auto_association_messages": {},
            "auto_association_signatures": {},
        }

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            data = self._default()
            self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return data

        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            data = self._default()

        merged = self._default()
        merged.update(data if isinstance(data, dict) else {})
        return merged

    def update(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        data = self.load()
        data.update(patch)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return data
