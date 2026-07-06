"""Small helpers shared across pipeline modules."""
import re


class PipelineCancelled(Exception):
    """Raised when the user cancels processing; unwinds the pipeline cleanly."""


def safe_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name)[:80].strip()
