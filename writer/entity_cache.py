import json
import re
from datetime import datetime, timezone
from pathlib import Path

CACHE_PATH = Path(__file__).parent.parent / "data" / "entity_cache.json"
_TTL_DAYS = 45


def _normalize(name: str) -> str:
    return re.sub(r"[^\w\s]", "", name.lower()).strip()


def load_cache() -> dict:
    try:
        if CACHE_PATH.exists():
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_cache(cache: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        print(f"[entity_cache] save failed: {e}")


def _is_fresh(record: dict) -> bool:
    try:
        last = datetime.fromisoformat(record["last_updated"]).replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).days < _TTL_DAYS
    except Exception:
        return False


def get_entity(cache: dict, name: str, resolved_sense: str | None = None) -> dict | None:
    """Return cached record if present and fresh (within TTL), else None.

    For ambiguous entities, resolved_sense must match a stored sense_label.
    Returns the matched sense entry or the flat record for unambiguous entities.
    """
    record = cache.get(_normalize(name))
    if not record:
        return None

    if "senses" in record:
        # Ambiguous entity — requires a sense match
        if not resolved_sense:
            return None
        for sense in record["senses"]:
            if sense.get("sense_label") == resolved_sense and _is_fresh(sense):
                return sense
        return None

    # Unambiguous entity
    return record if _is_fresh(record) else None


def set_entity(
    cache: dict,
    name: str,
    entity_type: str,
    summary: str,
    resolved_sense: str | None = None,
) -> None:
    """Write entity to in-memory cache.

    For ambiguous entities (resolved_sense provided), appends the new sense to
    the senses list — never overwrites an existing sense_label.
    For unambiguous entities, overwrites the flat record.
    """
    key = _normalize(name)
    now = datetime.now(timezone.utc).date().isoformat()

    if resolved_sense is not None:
        record = cache.get(key, {})
        senses = record.get("senses", [])
        if any(s.get("sense_label") == resolved_sense for s in senses):
            return  # Already cached — never overwrite
        senses.append({
            "sense_label": resolved_sense,
            "summary": summary,
            "last_updated": now,
        })
        cache[key] = {"senses": senses}
    else:
        cache[key] = {
            "canonical_name": name,
            "entity_type": entity_type,
            "summary": summary,
            "last_updated": now,
        }
