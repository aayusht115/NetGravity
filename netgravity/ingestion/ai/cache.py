"""
NetGravity — Extraction Cache
==============================
Stops us paying an LLM to re-read a document that has not changed.

WHY THIS EXISTS
---------------
Distributor column mappings were already cached (keyed by distributor, and
reused once a human confirms them). Contract extraction had no equivalent, so
every pipeline run re-sent every contract to the model and paid for it again —
even though a signed rate card does not change between runs.

KEYED BY CONTENT, NOT BY FILENAME
---------------------------------
The cache key is a hash of the document's extracted TEXT, not its name. That
matters: if somebody edits a rate card and re-uploads it under the same name,
a filename-keyed cache would hand back the old extraction and the pipeline
would silently price the network off a superseded contract. Hashing the text
means a changed document automatically misses the cache and gets re-read,
with no human having to remember to invalidate anything.

STUB RESULTS ARE NEVER CACHED
-----------------------------
This is the subtle one. In stub mode the "extraction" is canned demo data. If
that were written to the cache, then adding a real API key later would still
return the stubbed result on every cache hit — the pipeline would look live
and be fake, which is precisely the failure the ai_stubbed flag exists to
prevent. So only genuine model output is ever persisted here.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from netgravity.ingestion.schemas.contract import ContractRule
from netgravity.ingestion.storage.base import StorageBackend

CONTRACT_CACHE_PREFIX = "contract_extractions"
CACHE_ZONE = "standardized"

# Bump when the extraction prompt or ContractRule shape changes in a way that
# makes previously cached extractions untrustworthy. Old entries then miss
# rather than being served stale.
CACHE_SCHEMA_VERSION = "1"


def content_digest(text: str) -> str:
    """Stable short hash of the document text — the cache identity."""
    payload = f"v{CACHE_SCHEMA_VERSION}:{text}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def contract_cache_key(text: str) -> str:
    return f"{CONTRACT_CACHE_PREFIX}/{content_digest(text)}.json"


def load_cached_contract(text: str, storage: Optional[StorageBackend]
                         ) -> Optional[ContractRule]:
    """
    Return a previously extracted rule for this exact document text, or None.

    Any problem reading or parsing the cache is treated as a miss. A corrupt
    cache entry must never be able to break ingestion — the worst it can cost
    is one re-extraction.
    """
    if storage is None:
        return None
    try:
        raw = storage.get_text(CACHE_ZONE, contract_cache_key(text))
    except FileNotFoundError:
        return None
    except Exception:
        return None

    try:
        payload: Dict[str, Any] = json.loads(raw)
        rule = ContractRule.model_validate(payload["rule"])
    except Exception:
        return None

    # Defensive: a stub result should never have been written, but if an old
    # or hand-edited cache file contains one, refuse to serve it.
    if rule.extracted_by == "stub":
        return None
    return rule


def save_contract(rule: ContractRule, text: str,
                  storage: Optional[StorageBackend]) -> Optional[str]:
    """
    Persist a genuine extraction. Returns the locator, or None if not cached.

    Stub output is deliberately not written — see the module docstring.
    """
    if storage is None or rule.extracted_by == "stub":
        return None

    payload = {
        "cached_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "schema_version": CACHE_SCHEMA_VERSION,
        "extracted_by": rule.extracted_by,
        "source_file_key": rule.source_file_key,
        "rule": rule.model_dump(mode="json"),
    }
    try:
        return storage.save_text(
            CACHE_ZONE,
            contract_cache_key(text),
            json.dumps(payload, indent=2, default=str),
        )
    except Exception:
        # Failing to cache is not a reason to fail the run.
        return None
