from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from typing import List, Optional

@dataclass
class CanonicalService:
    id: str                         # CT event/appointment id or WT service id
    type_name: str                  # e.g. "Gottesdienst", "Hilft Ja Nix", "Atempause"
    name: str                       # Subtitle / specific service title
    start_time: str                 # Normalized local time: YYYY-MM-DDTHH:MM:SS
    notes: str = ""
    mod_timestamp: str = ""         # UTC ISO string
    is_canceled: bool = False

    def compute_hash(self) -> str:
        """Computes deterministic MD5 hash of service content (ignoring mod timestamp)."""
        content = {
            "type": (self.type_name or "").strip().lower(),
            "name": (self.name or "").strip(),
            "time": (self.start_time or "").strip()[:19],
            "notes": (self.notes or "").strip(),
            "is_canceled": bool(self.is_canceled)
        }
        serialized = json.dumps(content, sort_keys=True, ensure_ascii=False)
        return hashlib.md5(serialized.encode("utf-8")).hexdigest()
