"""Staleness reporting for the bundled yt-dlp, and diagnosis of its failures.

A version floor cannot catch extractor rot. YouTube changes first and yt-dlp
ships the fix afterwards, so a floor in requirements.txt only records what was
known when it was written: on 2026-09-16 the floor was >=2026.7.4 and the
installed 2026.07.04 satisfied it while every download returned 403.

Age is the signal that correlates. yt-dlp releases roughly weekly, and
_paths.ytdlp_command() re-execs this same interpreter against the bundled
yt_dlp package — which in a frozen build no user can upgrade — so a build's
yt-dlp only ever gets older than the site it has to keep up with.
"""
import datetime
import logging
import re
import sys

logger = logging.getLogger(__name__)

# yt-dlp releases roughly weekly, so 45 days is several releases behind without
# being so tight that a quiet month cries wolf. The version that broke was 74
# days old.
STALE_AFTER_DAYS = 45

_VERSION_RE = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})")

# All four of these mean "the extractor has aged out" in practice, but yt-dlp
# reports them as transport, format or anti-bot errors. Left untranslated, they
# send you debugging the network instead of the version.
_FAILURE_SIGNATURES = (
    (re.compile(r"HTTP Error 403|403: Forbidden"), "YouTube rejected the download (HTTP 403)"),
    (re.compile(r"Requested format is not available"), "YouTube offered no usable format"),
    (re.compile(r"nsig extraction failed|[Ss]ignature extraction failed"),
     "YouTube's signature scheme changed"),
    (re.compile(r"Sign in to confirm|not a bot"), "YouTube demanded sign-in verification"),
)


def installed_version() -> str | None:
    """Version of the yt_dlp package the pipeline will actually run.

    Imported rather than shelled out to: ytdlp_command() re-execs this same
    interpreter, so the importable package is by definition the one that runs.
    """
    try:
        from yt_dlp.version import __version__
        return __version__
    except Exception:
        logger.warning("Could not determine the bundled yt-dlp version", exc_info=True)
        return None


def release_date(version: str | None) -> datetime.date | None:
    """yt-dlp version strings are their release dates (2026.08.19)."""
    if not version:
        return None
    m = _VERSION_RE.match(version)
    if not m:
        return None
    try:
        return datetime.date(*(int(g) for g in m.groups()))
    except ValueError:
        return None


def age_days(version: str | None = None, today: datetime.date | None = None) -> int | None:
    """Days since the running yt-dlp was released, or None if undeterminable."""
    if version is None:
        version = installed_version()
    released = release_date(version)
    if released is None:
        return None
    return ((today or datetime.date.today()) - released).days


def _remedy() -> str:
    """A frozen build cannot upgrade its own yt-dlp; only a new build can."""
    if getattr(sys, "frozen", False):
        return "install a newer AutoNotes build"
    return "run: pip install -r requirements.txt"


def staleness_warning(
    version: str | None = None, today: datetime.date | None = None
) -> str | None:
    """Warning text when the bundled yt-dlp is old enough to be a risk.

    Returns None when it is current, or when the version cannot be parsed — an
    unrecognised version is not evidence of staleness, and a permanent false
    alarm would just train the warning out of usefulness.
    """
    if version is None:
        version = installed_version()
    age = age_days(version, today)
    if age is None or age < STALE_AFTER_DAYS:
        return None
    return (
        f"yt-dlp {version} is {age} days old. YouTube breaks older versions "
        f"without warning — if downloads fail, {_remedy()}."
    )


def diagnose(
    output_lines, version: str | None = None, today: datetime.date | None = None
) -> str | None:
    """Explain a yt-dlp failure, or None if it is not a recognised staleness symptom.

    Deliberately narrow: an unrecognised failure (a full disk, a private video)
    returns None so the caller keeps reporting the real error rather than
    blaming the version for everything that goes wrong.
    """
    for line in output_lines:
        for pattern, summary in _FAILURE_SIGNATURES:
            if pattern.search(line):
                if version is None:
                    version = installed_version()
                age = age_days(version, today)
                detail = (
                    f"the bundled yt-dlp ({version}) is {age} days old"
                    if age is not None
                    else "the bundled yt-dlp may have aged out"
                )
                return f"{summary}. This usually means {detail} — {_remedy()}."
    return None
