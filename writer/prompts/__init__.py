import hashlib
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent


def load_prompt(name: str) -> tuple[str, str]:
    """Load writer/prompts/<name>.txt, returning (prompt_text, version_id).

    version_id is the first 8 hex chars of the file's sha1 -- a cheap,
    automatic fingerprint so eval reports can record exactly which prompt
    text was tested without anyone manually bumping a version number.
    Trailing newlines are stripped since none of the original inline prompts
    ended with one; this keeps the loaded text identical to the string that
    used to live directly in synthesize.py.
    """
    path = _PROMPTS_DIR / f"{name}.txt"
    text = path.read_text(encoding="utf-8").rstrip("\n")
    version_id = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return text, version_id
