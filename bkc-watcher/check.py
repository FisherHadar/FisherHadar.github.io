#!/usr/bin/env python3
"""
bkc-watcher - watch Berkman Klein Center pages for a new call for applications.

Each run fetches every watched page, strips the HTML to plain text, and
compares it against the previous snapshot. It flags a page when:

  * the plain text changed at all, or
  * an "application signal" appears: the text now contains one of the
    open-call phrases AND no longer carries the "call has now closed" marker.

Standard library only - nothing to pip install. Network and parsing failures
are written to run.log and the run continues; the script does not crash.

Usage:
    check.py                 normal run (this is what the scheduler calls)
    check.py --self-test     exercise the whole pipeline on local fixtures
    check.py --force-notify  send a test notification, touch nothing else
    check.py --dry-run       fetch and compare, but write no snapshots/state
    check.py --status        show what is watched and the last known state
    check.py --reset         forget all snapshots and state (keeps the logs)
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import html.parser
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request

VERSION = "1.0"

# The folder that holds config, snapshots and logs. Defaults to the directory
# this script lives in, so the whole thing stays self-contained and movable.
HOME = Path(os.environ.get("BKC_WATCHER_HOME") or Path(__file__).resolve().parent)

CONFIG_PATH = HOME / "config.json"
SNAP_DIR = HOME / "snapshots"
ARCHIVE_DIR = SNAP_DIR / "archive"
STATE_PATH = HOME / "state.json"
RUN_LOG = HOME / "run.log"
FOUND_LOG = HOME / "found.log"

# ---------------------------------------------------------------------------
# Defaults. config.json is written on first run and is the file to edit.
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "_comment": "Edit this file to add/remove pages or tune behaviour. See README.md.",
    "user_agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "timeout_seconds": 30,
    "max_attempts": 3,
    "backoff_seconds": [5, 15, 45],
    "polite_delay_seconds": [2, 5],
    "notify": {
        "macos_notification": True,
        "always_write_found_log": True,
    },
    "signal_phrases": [
        "now accepting",
        "applications are open",
        "open call",
        "2027-2028",
        "2027-28",
        "apply by",
        "deadline",
    ],
    "closed_phrase": "this call for applications has now closed",
    "ignore_line_patterns": [
        r"^(©|copyright)\s*\d{4}",
        r"form_build_id|form_token",
        r"^\s*(printer-friendly|share this|subscribe)\s*$",
    ],
    "pages": [
        {
            "name": "fellowships",
            "url": "https://cyber.harvard.edu/getinvolved/fellowships",
            "notify_on_change": True,
        },
        {
            "name": "open-call-2026-2027",
            "url": "https://cyber.harvard.edu/page/open-call-fellowship-applications-2026-and-2026-2027",
            "notify_on_change": True,
        },
        {
            # Heads up: this page changes most weeks by design. If the weekly
            # "text changed" banner gets noisy, set notify_on_change to false
            # here - the keyword signal will still notify you.
            "name": "news",
            "url": "https://cyber.harvard.edu/news",
            "notify_on_change": True,
        },
    ],
}

# ---------------------------------------------------------------------------
# HTML -> plain text
# ---------------------------------------------------------------------------

_SKIP_TAGS = {
    "script", "style", "noscript", "svg", "head", "template",
    "iframe", "object", "canvas", "select",
}
_BLOCK_TAGS = {
    "p", "div", "br", "hr", "li", "tr", "td", "th", "dt", "dd",
    "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header",
    "footer", "nav", "aside", "main", "ul", "ol", "dl", "table",
    "blockquote", "figcaption", "form", "option", "address",
}


class _TextExtractor(html.parser.HTMLParser):
    """Collapse an HTML document into block-separated plain text lines.

    Inline tag boundaries are marked with a sentinel rather than dropped. That
    lets us produce two views of the same parse: a browser-faithful one for the
    snapshot (stable diffs) and a space-injected one for phrase matching, so
    markup like "We are <em>now</em> accepting" cannot hide a phrase from us.
    """

    SENTINEL = "\x00"

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def _boundary(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            return
        self._parts.append("\n" if tag in _BLOCK_TAGS else self.SENTINEL)

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        else:
            self._boundary(tag)

    def handle_startendtag(self, tag, attrs):
        self._boundary(tag)

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        else:
            self._boundary(tag)

    def handle_data(self, data):
        if self._skip_depth == 0 and data:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


_INLINE_WS = re.compile(r"[ \t\r\f\v\u00a0\u2007\u202f\u2009\u200a]+")
_DASHES = str.maketrans({
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "-", "\u2015": "-", "\u2212": "-",
})
_QUOTES = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"',
})


def extract(raw_html: str, ignore_patterns: list[str] | None = None) -> tuple[str, str]:
    """Strip HTML to plain text. Returns (snapshot_text, match_text).

    Normalisation matters twice over: it keeps week-to-week diffs free of
    cosmetic noise, and it means an en-dash "2027-28" on the page still
    matches the plain-hyphen phrase we are looking for.
    """
    parser = _TextExtractor()
    try:
        parser.feed(raw_html)
        parser.close()
    except Exception:
        # A malformed page should degrade, not explode: keep whatever we got.
        pass

    text = parser.text()
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_DASHES).translate(_QUOTES)

    patterns = [re.compile(p, re.IGNORECASE) for p in (ignore_patterns or [])]
    snap_lines: list[str] = []
    match_lines: list[str] = []
    for raw_line in text.split("\n"):
        snap = _INLINE_WS.sub(" ", raw_line.replace(_TextExtractor.SENTINEL, "")).strip()
        if not snap:
            continue
        if any(p.search(snap) for p in patterns):
            continue
        snap_lines.append(snap)
        match_lines.append(
            _INLINE_WS.sub(" ", raw_line.replace(_TextExtractor.SENTINEL, " ")).strip()
        )
    return "\n".join(snap_lines), "\n".join(match_lines)


def html_to_text(raw_html: str, ignore_patterns: list[str] | None = None) -> str:
    """Snapshot text only - convenience wrapper around extract()."""
    return extract(raw_html, ignore_patterns)[0]


def haystack(text: str) -> str:
    """Flatten to a single lowercase line so phrases match across line breaks."""
    return _INLINE_WS.sub(" ", text.replace("\n", " ")).lower().strip()


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_run(line: str) -> None:
    try:
        with RUN_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line.rstrip() + "\n")
    except Exception:
        pass  # logging must never be the thing that breaks the run
    print(line.rstrip())


def log_found(block: str) -> None:
    try:
        with FOUND_LOG.open("a", encoding="utf-8") as fh:
            fh.write(block.rstrip() + "\n")
    except Exception:
        pass


def run_line(name: str, status: str, **fields) -> str:
    tail = "  ".join(f"{k}={v}" for k, v in fields.items())
    return f"{now_iso()}  {name:<20.20} {status:<7.7} {tail}"


# ---------------------------------------------------------------------------
# Fetching - one request per page, real UA, retries with backoff
# ---------------------------------------------------------------------------

class FetchResult:
    def __init__(self, body: str | None, http_status: int, note: str = ""):
        self.body = body
        self.http_status = http_status
        self.note = note

    @property
    def not_modified(self) -> bool:
        return self.http_status == 304


def fetch(url: str, cfg: dict, cache: dict) -> FetchResult:
    """Fetch one URL. Returns a FetchResult or raises the final exception."""
    headers = {
        "User-Agent": cfg["user_agent"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "close",
    }
    # Conditional GET: if nothing changed the server sends 304 and no body.
    # Cheapest possible request for them, and an exact answer for us.
    if cache.get("etag"):
        headers["If-None-Match"] = cache["etag"]
    if cache.get("last_modified"):
        headers["If-Modified-Since"] = cache["last_modified"]

    attempts = max(1, int(cfg.get("max_attempts", 3)))
    backoff = list(cfg.get("backoff_seconds") or [5, 15, 45])
    last_exc: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            req = request.Request(url, headers=headers, method="GET")
            with request.urlopen(req, timeout=cfg.get("timeout_seconds", 30)) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                body = resp.read().decode(charset, errors="replace")
                cache["etag"] = resp.headers.get("ETag") or cache.get("etag")
                cache["last_modified"] = (
                    resp.headers.get("Last-Modified") or cache.get("last_modified")
                )
                return FetchResult(body, getattr(resp, "status", 200) or 200)

        except error.HTTPError as exc:
            if exc.code == 304:
                return FetchResult(None, 304, "not modified")
            retryable = exc.code == 429 or 500 <= exc.code < 600
            last_exc = exc
            if not retryable or attempt == attempts:
                raise
            wait = _retry_after(exc) or _backoff_for(backoff, attempt)
            log_run(run_line(url, "RETRY", http=exc.code, attempt=attempt, wait=f"{wait}s"))
            time.sleep(wait)

        except Exception as exc:  # URLError, socket timeout, SSL, decode...
            last_exc = exc
            if attempt == attempts:
                raise
            wait = _backoff_for(backoff, attempt)
            log_run(run_line(url, "RETRY", err=type(exc).__name__, attempt=attempt, wait=f"{wait}s"))
            time.sleep(wait)

    raise last_exc if last_exc else RuntimeError("fetch failed with no exception")


def _backoff_for(backoff: list, attempt: int) -> float:
    base = float(backoff[min(attempt - 1, len(backoff) - 1)]) if backoff else 5.0
    return round(base + random.uniform(0, base * 0.25), 1)  # jitter, so retries don't sync up


def _retry_after(exc: error.HTTPError) -> float | None:
    value = exc.headers.get("Retry-After") if exc.headers else None
    if not value:
        return None
    try:
        return min(float(value), 300.0)  # honour it, but never nap forever
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

def evaluate(snapshot_text: str, match_text: str, cfg: dict) -> dict:
    """Look for the phrases in both views of the parse.

    The two views disagree at inline tag boundaries: one joins "<em>now</em>
    accepting" correctly, the other joins "<span>2027</span>-28" correctly.
    A phrase counts if either view sees it - a false alarm costs one glance
    at a page, a missed call costs the whole application cycle.
    """
    hays = [haystack(snapshot_text), haystack(match_text)]
    hits = [p for p in cfg["signal_phrases"]
            if any(p.lower() in hay for hay in hays)]
    closed = any(cfg["closed_phrase"].lower() in hay for hay in hays)
    return {
        "hits": hits,
        "closed_marker": closed,
        "signal_open": bool(hits) and not closed,
    }


def diff_summary(old: str, new: str, context_lines: int = 12) -> tuple[int, int, str]:
    old_lines = old.split("\n")
    new_lines = new.split("\n")
    diff = list(difflib.unified_diff(old_lines, new_lines, lineterm="", n=0))
    added = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
    removed = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
    body = [d for d in diff if d[:1] in "+-" and not d.startswith(("+++", "---"))]
    excerpt = "\n".join("  " + line[:300] for line in body[:context_lines])
    if len(body) > context_lines:
        excerpt += f"\n  ... {len(body) - context_lines} more changed line(s)"
    return added, removed, excerpt


def reasons_for(page_name: str, prev: dict, curr: dict, changed: bool,
                added: int, removed: int, notify_on_change: bool,
                first_run: bool) -> tuple[list[str], bool]:
    """Return (human-readable reasons, whether to actually notify).

    Two tiers on purpose. The keyword signal is the thing worth interrupting
    you for. A bare text diff is always recorded, but a page like /news
    changes every week, so notify_on_change lets you mute that tier per page.
    """
    reasons: list[str] = []
    loud = False

    if first_run:
        if curr["signal_open"]:
            reasons.append(
                "FIRST RUN, and the page already shows an open-call signal: "
                + ", ".join(f'"{h}"' for h in curr["hits"])
                + " (closed-marker absent)"
            )
            loud = True
        return reasons, loud

    newly_open = curr["signal_open"] and not prev.get("signal_open", False)
    hits_changed = (
        curr["signal_open"]
        and set(curr["hits"]) != set(prev.get("hits", []))
    )
    closed_dropped = prev.get("closed_marker", False) and not curr["closed_marker"]

    if newly_open:
        reasons.append(
            "APPLICATION SIGNAL: now matches "
            + ", ".join(f'"{h}"' for h in curr["hits"])
            + " and no longer says the call has closed"
        )
        loud = True
    elif hits_changed:
        gained = sorted(set(curr["hits"]) - set(prev.get("hits", [])))
        reasons.append(
            "signal phrases changed; new match(es): "
            + ", ".join(f'"{h}"' for h in gained)
        )
        loud = True

    if closed_dropped and not newly_open:
        reasons.append('the "call has now closed" marker was REMOVED from this page')
        loud = True

    if changed:
        reasons.append(f"text changed: +{added} / -{removed} line(s)")
        if notify_on_change:
            loud = True

    return reasons, loud


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

def _osa_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def notify(title: str, subtitle: str, message: str, cfg: dict) -> str:
    """Post a desktop notification. Returns a short status for the run log."""
    if not cfg.get("notify", {}).get("macos_notification", True):
        return "disabled"

    message = message.replace("\n", " ")[:240]

    if sys.platform == "darwin" and shutil.which("osascript"):
        script = (
            f'display notification "{_osa_quote(message)}" '
            f'with title "{_osa_quote(title)}" '
            f'with subtitle "{_osa_quote(subtitle)}" '
            f'sound name "Glass"'
        )
        try:
            proc = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=20,
            )
            if proc.returncode == 0:
                return "sent"
            return f"failed:{proc.stderr.strip()[:80] or proc.returncode}"
        except Exception as exc:
            return f"failed:{type(exc).__name__}"

    # Not on a Mac: try the Linux equivalent, otherwise found.log is the record.
    if shutil.which("notify-send"):
        try:
            subprocess.run(
                ["notify-send", f"{title} - {subtitle}", message],
                capture_output=True, timeout=20,
            )
            return "sent:notify-send"
        except Exception as exc:
            return f"failed:{type(exc).__name__}"

    return "unavailable:found.log-only"


# ---------------------------------------------------------------------------
# Config / state
# ---------------------------------------------------------------------------

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except Exception as exc:
        log_run(run_line(path.name, "WARN", err=f"unreadable ({type(exc).__name__}), using defaults"))
        return default


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)  # atomic, so an interrupted run can't corrupt state


def load_config() -> dict:
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        save_json(CONFIG_PATH, DEFAULT_CONFIG)
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        log_run(run_line("config.json", "INIT", pages=len(cfg["pages"])))
    for key, value in DEFAULT_CONFIG.items():
        cfg.setdefault(key, value)
    return cfg


# ---------------------------------------------------------------------------
# One page
# ---------------------------------------------------------------------------

def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "page"


def check_page(page: dict, cfg: dict, state: dict, dry_run: bool) -> dict:
    """Check one page. Never raises - failures come back as a result dict."""
    name = page.get("name") or slug(page["url"])
    url = page["url"]
    prev = state.get(name, {})
    snap_path = SNAP_DIR / f"{slug(name)}.txt"
    started = time.monotonic()

    cache = {"etag": prev.get("etag"), "last_modified": prev.get("last_modified")}

    try:
        result = fetch(url, cfg, cache)
    except Exception as exc:
        detail = getattr(exc, "code", None) or getattr(exc, "reason", None) or str(exc)
        log_run(run_line(name, "ERROR", changed="unknown", signal="unknown",
                         err=f"{type(exc).__name__}: {str(detail)[:120]}",
                         secs=f"{time.monotonic() - started:.1f}"))
        return {"name": name, "ok": False, "reasons": [], "notify": False}

    if result.not_modified:
        # Nothing was re-parsed, so report the last known signal state rather
        # than "no" - otherwise the log reads as if an open call had vanished.
        prev_signal = prev.get("signal_open")
        log_run(run_line(name, "OK", changed="no",
                         signal=("yes" if prev_signal else "no" if prev_signal is not None else "unknown"),
                         http=304, note="not-modified",
                         secs=f"{time.monotonic() - started:.1f}"))
        if not dry_run:
            state[name] = {**prev, **cache, "last_checked": now_iso()}
        return {"name": name, "ok": True, "reasons": [], "notify": False}

    text, match_text = extract(result.body or "", cfg.get("ignore_line_patterns"))
    if len(text) < 200:
        # A near-empty page usually means a block page or a broken fetch, not
        # a real deletion. Refuse to overwrite a good snapshot with rubbish.
        log_run(run_line(name, "WARN", changed="unknown", signal="unknown",
                         chars=len(text), note="suspiciously-short, snapshot kept",
                         secs=f"{time.monotonic() - started:.1f}"))
        return {"name": name, "ok": False, "reasons": [], "notify": False}

    curr = evaluate(text, match_text, cfg)
    new_hash = sha(text)
    first_run = not snap_path.exists()
    old_text = "" if first_run else snap_path.read_text(encoding="utf-8")
    changed = (not first_run) and (sha(old_text) != new_hash)
    added, removed, excerpt = (0, 0, "")
    if changed:
        added, removed, excerpt = diff_summary(old_text, text)

    reasons, loud = reasons_for(
        name, prev, curr, changed, added, removed,
        page.get("notify_on_change", True), first_run,
    )

    # Don't re-announce the identical page state on a later run.
    already = prev.get("notified_hash") == new_hash and prev.get("notified_reasons") == reasons
    if already:
        loud = False

    if not dry_run:
        SNAP_DIR.mkdir(parents=True, exist_ok=True)
        if changed:
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            (ARCHIVE_DIR / f"{slug(name)}-{stamp}.txt").write_text(old_text, encoding="utf-8")
        snap_path.write_text(text, encoding="utf-8")
        state[name] = {
            "url": url,
            "hash": new_hash,
            "hits": curr["hits"],
            "closed_marker": curr["closed_marker"],
            "signal_open": curr["signal_open"],
            "chars": len(text),
            "last_checked": now_iso(),
            "last_changed": now_iso() if changed else prev.get("last_changed"),
            "etag": cache.get("etag"),
            "last_modified": cache.get("last_modified"),
            "notified_hash": new_hash if reasons else prev.get("notified_hash"),
            "notified_reasons": reasons if reasons else prev.get("notified_reasons"),
        }

    status = "FIRST" if first_run else "OK"
    log_run(run_line(
        name, status,
        changed=("first-snapshot" if first_run else ("yes" if changed else "no")),
        signal=("yes" if curr["signal_open"] else "no"),
        chars=len(text), http=result.http_status,
        hits=len(curr["hits"]), closed=("yes" if curr["closed_marker"] else "no"),
        secs=f"{time.monotonic() - started:.1f}",
    ))

    if reasons and cfg.get("notify", {}).get("always_write_found_log", True) and not dry_run:
        log_found(
            "=" * 78 + f"\n{now_iso()}  FLAGGED  {name}\nURL: {url}\nReasons:\n"
            + "\n".join(f"  - {r}" for r in reasons)
            + (f"\nDiff excerpt:\n{excerpt}" if excerpt else "")
            + (f"\n(notification suppressed: identical to the last one sent)" if already else "")
            + "\n" + "=" * 78
        )

    return {"name": name, "ok": True, "url": url, "reasons": reasons,
            "notify": loud, "signal": curr["signal_open"]}


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def do_run(cfg: dict, dry_run: bool) -> int:
    HOME.mkdir(parents=True, exist_ok=True)
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    state = load_json(STATE_PATH, {})
    pages = cfg.get("pages", [])

    log_run("-" * 78)
    log_run(run_line("RUN", "START", pages=len(pages), version=VERSION,
                     mode=("dry-run" if dry_run else "normal")))

    results = []
    delay_lo, delay_hi = (cfg.get("polite_delay_seconds") or [2, 5])[:2]
    for index, page in enumerate(pages):
        if index:  # space the requests out; we are a guest on their server
            time.sleep(random.uniform(float(delay_lo), float(delay_hi)))
        results.append(check_page(page, cfg, state, dry_run))

    if not dry_run:
        save_json(STATE_PATH, state)

    flagged = [r for r in results if r.get("notify")]
    errors = [r for r in results if not r.get("ok")]

    if flagged and not dry_run:
        loud = [r for r in flagged if r.get("signal")]
        title = "BKC fellowships: possible new call" if loud else "BKC pages changed"
        subtitle = ", ".join(r["name"] for r in flagged)
        message = flagged[0]["reasons"][0] if flagged[0]["reasons"] else "see found.log"
        status = notify(title, subtitle, message, cfg)
        log_run(run_line("NOTIFY", "SENT" if status.startswith("sent") else "WARN",
                         result=status, pages=subtitle))

    log_run(run_line("RUN", "END", checked=len(results), flagged=len(flagged),
                     errors=len(errors)))
    return 0  # always 0: a failed fetch is logged, not a scheduler alarm


def do_status(cfg: dict) -> int:
    state = load_json(STATE_PATH, {})
    print(f"bkc-watcher {VERSION}   home: {HOME}")
    print(f"snapshots: {SNAP_DIR}")
    print()
    for page in cfg.get("pages", []):
        name = page.get("name") or slug(page["url"])
        s = state.get(name, {})
        print(f"  {name}")
        print(f"    url            {page['url']}")
        print(f"    notify on diff {page.get('notify_on_change', True)}")
        print(f"    last checked   {s.get('last_checked', 'never')}")
        print(f"    last changed   {s.get('last_changed', 'never')}")
        print(f"    signal open    {s.get('signal_open', 'unknown')}")
        print(f"    matched        {', '.join(s.get('hits', [])) or '-'}")
        print(f"    closed marker  {s.get('closed_marker', 'unknown')}")
        print()
    return 0


def do_reset() -> int:
    for path in (STATE_PATH,):
        path.unlink(missing_ok=True)
    if SNAP_DIR.exists():
        shutil.rmtree(SNAP_DIR)
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    log_run(run_line("RESET", "OK", note="snapshots and state cleared"))
    return 0


FIXTURE_CLOSED = """
<html><head><title>Fellowships</title><style>.x{color:red}</style></head><body>
<nav>Berkman Klein Center</nav>
<h1>Fellowship Program</h1>
<p>The Berkman Klein Center fellowship program brings together scholars.</p>
<p>This call for applications has now closed. We received applications for the
2026 and 2026&ndash;2027 fellowship year.</p>
<script>var tracker = 1;</script>
<footer>&copy; 2026 President and Fellows of Harvard College</footer>
</body></html>
"""

FIXTURE_OPEN = """
<html><head><title>Fellowships</title></head><body>
<nav>Berkman Klein Center</nav>
<h1>Fellowship Program</h1>
<p>The Berkman Klein Center fellowship program brings together scholars.</p>
<p>We are <em>now</em> accepting applications for the <span>2027</span>&ndash;28
fellowship year. <strong>Apply by</strong> January 15. The deadline is firm.</p>
<footer>&copy; 2026 President and Fellows of Harvard College</footer>
</body></html>
"""


def do_self_test(cfg: dict) -> int:
    """Run the detection pipeline on fixtures. Touches no real snapshot."""
    print("bkc-watcher self-test\n" + "-" * 40)

    closed_text, closed_match = extract(FIXTURE_CLOSED, cfg.get("ignore_line_patterns"))
    open_text, open_match = extract(FIXTURE_OPEN, cfg.get("ignore_line_patterns"))

    print("1. HTML -> text (script/style/copyright dropped, en-dash normalised):")
    for line in closed_text.split("\n"):
        print("   |", line)

    before = evaluate(closed_text, closed_match, cfg)
    after = evaluate(open_text, open_match, cfg)
    print(f"\n2. Before: hits={before['hits']} closed_marker={before['closed_marker']} "
          f"signal_open={before['signal_open']}")
    print(f"   After:  hits={after['hits']} closed_marker={after['closed_marker']} "
          f"signal_open={after['signal_open']}")

    added, removed, excerpt = diff_summary(closed_text, open_text)
    print(f"\n3. Diff: +{added} / -{removed}")
    print(excerpt)

    prev = {"hits": before["hits"], "closed_marker": before["closed_marker"],
            "signal_open": before["signal_open"]}
    reasons, loud = reasons_for("fixture", prev, after, True, added, removed, True, False)
    print("\n4. Reasons:")
    for reason in reasons:
        print("   -", reason)
    print(f"   would notify: {loud}")

    checks = [
        ("closed page must NOT signal", before["signal_open"] is False),
        ("open page MUST signal", after["signal_open"] is True),
        ("en-dash 2027-28 matched across <span>", "2027-28" in after["hits"]),
        ('"now accepting" matched across <em>', "now accepting" in after["hits"]),
        ('"apply by" matched across <strong>', "apply by" in after["hits"]),
        ("closed marker gone", after["closed_marker"] is False),
        ("script/style stripped", "tracker" not in closed_text),
        ("copyright line ignored", "President and Fellows" not in closed_text),
        ("transition triggers notify", loud is True),
        ("no repeat on identical state",
         reasons_for("f", {**prev, **after}, after, False, 0, 0, True, False)[1] is False),
    ]
    print("\n5. Assertions:")
    failed = 0
    for label, passed in checks:
        print(f"   [{'PASS' if passed else 'FAIL'}] {label}")
        failed += 0 if passed else 1
    print("\n" + ("ALL CHECKS PASSED" if not failed else f"{failed} CHECK(S) FAILED"))
    return 0 if not failed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Watch BKC pages for a new call for applications.")
    parser.add_argument("--dry-run", action="store_true", help="compare but write nothing")
    parser.add_argument("--self-test", action="store_true", help="exercise the rules on fixtures")
    parser.add_argument("--force-notify", action="store_true", help="send a test notification")
    parser.add_argument("--status", action="store_true", help="show watched pages and last state")
    parser.add_argument("--reset", action="store_true", help="clear snapshots and state")
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
        if args.self_test:
            return do_self_test(cfg)
        if args.status:
            return do_status(cfg)
        if args.reset:
            return do_reset()
        if args.force_notify:
            status = notify("BKC watcher", "test notification",
                            "If you can see this, notifications work.", cfg)
            log_run(run_line("NOTIFY", "TEST", result=status))
            return 0
        return do_run(cfg, args.dry_run)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # Last line of defence: a scheduled job should complain to the log,
        # not die silently or pop a Python traceback dialog.
        log_run(run_line("RUN", "FATAL", err=f"{type(exc).__name__}: {str(exc)[:200]}"))
        return 0


if __name__ == "__main__":
    sys.exit(main())
