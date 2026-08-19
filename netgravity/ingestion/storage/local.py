"""
NetGravity — Local Filesystem Storage Backend
==============================================
Used for local development and the prototype. Mirrors the exact zone/key
layout that Azure Blob containers will use, so keys are portable unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from netgravity.ingestion.storage.base import StorageBackend


class LocalStorage(StorageBackend):
    def __init__(self, root: Path):
        self.root = Path(root)

    def _resolve(self, zone: str, key: str) -> Path:
        path = (self.root / zone / key).resolve()
        # Defensive: never allow a key to escape its zone via "../"
        zone_root = (self.root / zone).resolve()
        if zone_root not in path.parents and path != zone_root:
            raise ValueError(f"Refusing key that escapes zone '{zone}': {key}")
        return path

    def save(self, zone: str, key: str, data: bytes) -> str:
        path = self._resolve(zone, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def get(self, zone: str, key: str) -> bytes:
        path = self._resolve(zone, key)
        if not path.exists():
            raise FileNotFoundError(f"No object at zone='{zone}' key='{key}' ({path})")
        return path.read_bytes()

    def exists(self, zone: str, key: str) -> bool:
        return self._resolve(zone, key).exists()

    def list(self, zone: str, prefix: str = "") -> List[str]:
        zone_root = self.root / zone
        if not zone_root.exists():
            return []
        keys: List[str] = []
        for p in sorted(zone_root.rglob("*")):
            if p.is_file() and p.name != ".gitkeep":
                key = p.relative_to(zone_root).as_posix()
                if key.startswith(prefix):
                    keys.append(key)
        return keys

    def locator(self, zone: str, key: str) -> str:
        return str(self.root / zone / key)
