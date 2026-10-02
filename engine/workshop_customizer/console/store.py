"""The console's local state: one JSON file per collection under the console data dir, written atomically."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def path(self, name: str) -> Path:
        return self.root / f"{name}.json"

    def read(self, name: str, default: Any) -> Any:
        """The collection, or ``default`` when it does not exist yet. Any other failure (too many open files, a file
        that is not JSON) is raised: read as empty, the users file would open the console and an update would write
        the empty collection over the real one."""
        try:
            text = self.path(name).read_text(encoding="utf-8")
        except FileNotFoundError:
            return default
        return json.loads(text)

    def exists(self, name: str) -> bool:
        return self.path(name).exists()

    def write(self, name: str, value: Any) -> None:
        with self._lock:
            path = self.path(name)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(tmp, path)

    def update(self, name: str, default: Any, change) -> Any:
        """Read-modify-write under the store's lock; ``change(value)`` returns the new value."""
        with self._lock:
            try:
                value = json.loads(self.path(name).read_text(encoding="utf-8"))
            except FileNotFoundError:
                value = default
            value = change(value)
            path = self.path(name)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(tmp, path)
            return value
