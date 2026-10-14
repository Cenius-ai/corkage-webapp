#!/usr/bin/env python3
"""Corkage - a one-file cellar log for cork condition, cellar climate and reminders.

Corkage keeps three append-only logs in a single JSON store guarded by one local
cellar passphrase:

  * cork entries    - the state of a bottle's cork plus the extraction note
  * cellar readings - temperature (C) and relative humidity (%)
  * reminders       - soak and decant nudges, some created automatically

Everything is standard library only, offline, and one short-lived process per
command.  Exit codes: 0 ok, 2 usage or validation, 3 cellar unlock or store,
1 unexpected.

Design tokens (committed direction - calm-precise, terminal dark, system
monospace, aligned columns, exactly one accent):
  accent   oklch(0.58 0.12 279)  ==  #6c71c0
  surface  terminal dark; colour is never the only signal
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import math
import os
import re
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

PROG = "corkage"
VERSION = "1.0.0"

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_USAGE = 2
EXIT_STORE = 3

CONDITIONS: Tuple[str, ...] = ("sound", "soaked", "crumbly", "moldy")
AUTO_DECANT_CONDITIONS: Tuple[str, ...] = ("soaked", "crumbly")
REMINDER_KINDS: Tuple[str, ...] = ("soak", "decant")
GROUP_ACTIONS: Dict[str, Tuple[str, ...]] = {
    "cork": ("add", "list"),
    "reading": ("add", "list"),
    "reminder": ("add", "list", "done"),
}

AUTO_DECANT_DAYS = 30
MIN_PASSPHRASE = 4
SALT_BYTES = 16
PBKDF2_ITERATIONS = 120_000
STORE_VERSION = 1

TEMP_MIN = -5.0
TEMP_MAX = 45.0
HUMIDITY_MIN = 0
HUMIDITY_MAX = 100

ENV_HOME = "CORKAGE_HOME"
ENV_PASSPHRASE = "CORKAGE_PASSPHRASE"
ENV_DEBUG = "CORKAGE_DEBUG"
ENV_COLOR = "CORKAGE_COLOR"

DEMO_PASSPHRASE = "cenius"

MSG_NO_CELLAR = "no cellar yet - run corkage init first"
MSG_UNLOCK_FAILED = "could not unlock the cellar (wrong passphrase)"
MSG_PASSPHRASE_SHORT = "passphrase needs at least 4 characters"
MSG_PASSPHRASE_MISMATCH = "passphrases did not match - try again"
MSG_NO_PASSPHRASE = (
    "no passphrase given - use --passphrase or set CORKAGE_PASSPHRASE"
)
MSG_ALREADY_INITIALISED = "cellar already initialised at {path}"
MSG_CORRUPT_STORE = "cellar file at {path} could not be read ({reason})"
MSG_NEWER_STORE = (
    "cellar file at {path} was written by a newer corkage (store version {version})"
)

EMPTY_CORK = (
    "No cork entries yet - add one with: "
    "corkage cork add --bottle NAME --condition sound"
)
EMPTY_READING = (
    "No cellar readings yet - add one with: "
    "corkage reading add --temp C --humidity PCT"
)
EMPTY_REMINDER = (
    "No reminders yet - add one with: "
    "corkage reminder add --kind soak --due YYYY-MM-DD --bottle NAME"
)

# #6c71c0 as a 24-bit ANSI foreground colour; used only when stdout is a TTY.
ACCENT_ANSI = "\x1b[38;2;108;113;192m"
RESET_ANSI = "\x1b[0m"

BOOL_NO_COLOR = os.environ.get("NO_COLOR") is not None
_COLOR_CACHE: Optional[bool] = None


class CorkageError(Exception):
    """Base class for the two failure classes Corkage reports itself."""

    exit_code = EXIT_UNEXPECTED

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UsageError(CorkageError):
    """A usage or validation failure: exit 2."""

    exit_code = EXIT_USAGE

    def __init__(self, message: str, show_usage: bool = False) -> None:
        super().__init__(message)
        self.show_usage = show_usage


class StoreError(CorkageError):
    """A cellar unlock or store failure: exit 3."""

    exit_code = EXIT_STORE


_ACTIVE_USAGE: Optional[str] = None


@dataclass
class CorkEntry:
    """One bottle's cork condition and extraction note."""

    id: int
    bottle: str
    condition: str
    note: str
    created_at: str

    def to_json(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "bottle": self.bottle,
            "condition": self.condition,
            "note": self.note,
            "created_at": self.created_at,
        }

    @classmethod
    def from_json(cls, raw: Dict[str, Any]) -> "CorkEntry":
        return cls(
            id=_as_int(raw.get("id")),
            bottle=_as_text(raw.get("bottle")),
            condition=_as_text(raw.get("condition")),
            note=_as_text(raw.get("note")),
            created_at=_as_text(raw.get("created_at")),
        )


@dataclass
class Reading:
    """One cellar temperature and humidity reading."""

    id: int
    temp_c: float
    humidity_pct: int
    note: str
    recorded_at: str

    def to_json(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "temp_c": self.temp_c,
            "humidity_pct": self.humidity_pct,
            "note": self.note,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_json(cls, raw: Dict[str, Any]) -> "Reading":
        return cls(
            id=_as_int(raw.get("id")),
            temp_c=_as_float(raw.get("temp_c")),
            humidity_pct=_as_int(raw.get("humidity_pct")),
            note=_as_text(raw.get("note")),
            recorded_at=_as_text(raw.get("recorded_at")),
        )


@dataclass
class Reminder:
    """A soak or decant nudge; pending while done_at is None."""

    id: int
    kind: str
    bottle: str
    due_date: str
    note: str
    created_at: str
    done_at: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "bottle": self.bottle,
            "due_date": self.due_date,
            "note": self.note,
            "created_at": self.created_at,
            "done_at": self.done_at,
        }

    @classmethod
    def from_json(cls, raw: Dict[str, Any]) -> "Reminder":
        done_at = raw.get("done_at")
        return cls(
            id=_as_int(raw.get("id")),
            kind=_as_text(raw.get("kind")),
            bottle=_as_text(raw.get("bottle")),
            due_date=_as_text(raw.get("due_date")),
            note=_as_text(raw.get("note")),
            created_at=_as_text(raw.get("created_at")),
            done_at=_as_text(done_at) if done_at else None,
        )


@dataclass
class CellarStore:
    """The whole cellar: passphrase record plus the three append-only logs."""

    store_version: int = STORE_VERSION
    created_at: str = ""
    passphrase_salt: str = ""
    passphrase_hash: str = ""
    corks: List[CorkEntry] = field(default_factory=list)
    readings: List[Reading] = field(default_factory=list)
    reminders: List[Reminder] = field(default_factory=list)
    next_ids: Dict[str, int] = field(
        default_factory=lambda: {"cork": 1, "reading": 1, "reminder": 1}
    )

    def to_json(self) -> Dict[str, Any]:
        return {
            "store_version": self.store_version,
            "created_at": self.created_at,
            "passphrase_salt": self.passphrase_salt,
            "passphrase_hash": self.passphrase_hash,
            "next_ids": dict(self.next_ids),
            "corks": [entry.to_json() for entry in self.corks],
            "readings": [reading.to_json() for reading in self.readings],
            "reminders": [reminder.to_json() for reminder in self.reminders],
        }


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_timestamp(moment: datetime) -> str:
    """Store timestamps as UTC ISO 8601 with a trailing Z."""

    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def display_time(stamp: str) -> str:
    """Render a stored UTC stamp as '2024-06-01 12:00 UTC' for the tables."""

    text = (stamp or "").strip()
    if not text:
        return "unknown"
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def format_number(value: float) -> str:
    """Compact numeric formatting for messages: 90.0 -> '90'."""

    return f"{value:g}"


def color_enabled() -> bool:
    global _COLOR_CACHE
    if _COLOR_CACHE is None:
        preference = os.environ.get(ENV_COLOR, "").strip().lower()
        if BOOL_NO_COLOR or preference in ("never", "off", "0"):
            _COLOR_CACHE = False
        elif preference in ("always", "on", "1"):
            _COLOR_CACHE = True
        else:
            _COLOR_CACHE = sys.stdout.isatty()
    return _COLOR_CACHE


def accent(text: str) -> str:
    return f"{ACCENT_ANSI}{text}{RESET_ANSI}" if color_enabled() and text else text


def clamp(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def render_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    highlight: Iterable[str] = (),
) -> str:
    """Aligned, monospace columns with an ASCII rule - no chrome, no colour-only signals."""

    marked = set(highlight)
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = [
        accent("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(headers)).rstrip())
    ]
    lines.append("-" * min(sum(widths) + 2 * (len(widths) - 1), 78))
    for row in rows:
        cells = []
        for index, cell in enumerate(row):
            padded = cell.ljust(widths[index])
            cells.append(accent(padded) if cell in marked else padded)
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines)


def cellar_home() -> str:
    """$CORKAGE_HOME, defaulting to ~/.corkage."""

    configured = os.environ.get(ENV_HOME, "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    return os.path.join(os.path.expanduser("~"), ".corkage")


def store_path() -> str:
    return os.path.join(cellar_home(), "store.json")


def load_store(path: str) -> CellarStore:
    """Read the JSON store, tolerating missing optional keys but not corruption."""

    try:
        # Plain JSON, parsed in-process with the safe stdlib parser; the parsed
        # value must be an object and every field is coerced below before use.
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.loads(handle.read())
    except FileNotFoundError:
        raise StoreError(MSG_NO_CELLAR) from None
    except OSError as exc:
        raise StoreError(
            MSG_CORRUPT_STORE.format(path=path, reason=exc.strerror or exc)
        ) from None
    except ValueError:
        raise StoreError(
            MSG_CORRUPT_STORE.format(path=path, reason="not valid JSON")
        ) from None
    if not isinstance(raw, dict):
        raise StoreError(MSG_CORRUPT_STORE.format(path=path, reason="not a JSON object"))

    version = _as_int(raw.get("store_version")) or STORE_VERSION
    if version > STORE_VERSION:
        raise StoreError(MSG_NEWER_STORE.format(path=path, version=version))

    store = CellarStore(
        store_version=STORE_VERSION,
        created_at=_as_text(raw.get("created_at")),
        passphrase_salt=_as_text(raw.get("passphrase_salt")),
        passphrase_hash=_as_text(raw.get("passphrase_hash")),
        corks=[CorkEntry.from_json(item) for item in _as_list(raw.get("corks"))],
        readings=[Reading.from_json(item) for item in _as_list(raw.get("readings"))],
        reminders=[Reminder.from_json(item) for item in _as_list(raw.get("reminders"))],
    )
    store.next_ids = _normalise_ids(store, raw.get("next_ids"))
    return store


def _as_list(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _normalise_ids(store: CellarStore, raw_ids: Any) -> Dict[str, int]:
    """Keep the id counters ahead of every stored id, whatever the file says."""

    stored = raw_ids if isinstance(raw_ids, dict) else {}
    highest = {
        "cork": max([entry.id for entry in store.corks] or [0]),
        "reading": max([reading.id for reading in store.readings] or [0]),
        "reminder": max([reminder.id for reminder in store.reminders] or [0]),
    }
    return {
        kind: max(_as_int(stored.get(kind)) or 1, highest[kind] + 1)
        for kind in ("cork", "reading", "reminder")
    }


def save_store(path: str, store: CellarStore) -> None:
    """Atomic write: temp file in the same directory, fsync, then os.replace."""

    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        payload = json.dumps(store.to_json(), indent=2) + "\n"
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=directory,
            prefix=".store-",
            suffix=".tmp",
            delete=False,
        )
        temp_path = handle.name
        try:
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        except OSError:
            _remove_quietly(temp_path)
            raise
    except OSError as exc:
        raise StoreError(
            "could not write the cellar file at {path} ({reason})".format(
                path=path, reason=exc.strerror or exc
            )
        ) from None
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _remove_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def next_id(store: CellarStore, kind: str) -> int:
    current = store.next_ids.get(kind) or 1
    store.next_ids[kind] = current + 1
    return current


def derive_hash(passphrase: str, salt: bytes) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return digest.hex()


def make_passphrase_record(passphrase: str) -> Tuple[str, str]:
    salt = os.urandom(SALT_BYTES)
    return salt.hex(), derive_hash(passphrase, salt)


def verify_passphrase(store: CellarStore, passphrase: str) -> bool:
    try:
        salt = bytes.fromhex(store.passphrase_salt)
    except ValueError:
        return False
    if not store.passphrase_hash:
        return False
    candidate = derive_hash(passphrase, salt)
    return hmac.compare_digest(candidate, store.passphrase_hash)


def prompt_passphrase(label: str) -> str:
    """Hidden prompt on a terminal, one stdin line when there is no terminal."""

    if sys.stdin.isatty():
        try:
            return getpass.getpass(label)
        except EOFError:
            raise StoreError(MSG_NO_PASSPHRASE) from None
        except KeyboardInterrupt:
            raise StoreError("passphrase entry cancelled") from None
    print(label, end="", file=sys.stderr, flush=True)
    line = sys.stdin.readline()
    if line == "":
        raise StoreError(MSG_NO_PASSPHRASE)
    return line.rstrip("\r\n")


def passphrase_from_request(args: argparse.Namespace) -> Optional[str]:
    supplied = getattr(args, "passphrase", None)
    if supplied:
        return supplied
    from_env = os.environ.get(ENV_PASSPHRASE, "")
    return from_env if from_env else None


def unlock_cellar(args: argparse.Namespace) -> Tuple[str, CellarStore]:
    """Open the store, verify the passphrase, and return (path, store)."""

    path = store_path()
    if not os.path.exists(path):
        raise StoreError(MSG_NO_CELLAR)
    store = load_store(path)
    passphrase = passphrase_from_request(args)
    if passphrase is None:
        passphrase = prompt_passphrase("cellar passphrase: ")
    if not verify_passphrase(store, passphrase):
        raise StoreError(MSG_UNLOCK_FAILED)
    return path, store


def emit_error(message: str, usage: Optional[str] = None) -> None:
    """One line to stderr, optionally followed by the command's usage."""

    print(message, file=sys.stderr)
    if usage:
        print(usage, file=sys.stderr)


def require_text(raw: Optional[str], flag: str, description: str) -> str:
    if raw is None:
        raise UsageError(f"missing required {flag} ({description})", show_usage=True)
    value = raw.strip()
    if not value:
        raise UsageError(f"{flag} cannot be empty ({description})")
    return value


def require_choice(
    raw: Optional[str], flag: str, description: str, allowed: Sequence[str]
) -> str:
    value = require_text(raw, flag, description)
    lowered = value.lower()
    if lowered not in allowed:
        raise UsageError(
            f"unknown {flag} {value!r} - choose one of: {', '.join(allowed)}"
        )
    return lowered


def parse_temperature(raw: Optional[str]) -> float:
    value = require_text(raw, "--temp", "Celsius temperature")
    try:
        temp = float(value)
    except ValueError:
        raise UsageError(f"--temp needs a number like 13.5 (got {value!r})") from None
    if not math.isfinite(temp):
        raise UsageError(f"--temp needs a number like 13.5 (got {value!r})")
    if not (TEMP_MIN <= temp <= TEMP_MAX):
        raise UsageError(
            "--temp must be between {low} and {high} C (got {seen})".format(
                low=format_number(TEMP_MIN), high=format_number(TEMP_MAX),
                seen=format_number(temp),
            )
        )
    return round(temp, 1)


def parse_humidity(raw: Optional[str]) -> int:
    value = require_text(raw, "--humidity", "relative humidity in percent")
    try:
        number = float(value)
    except ValueError:
        raise UsageError(
            f"--humidity needs a number like 68 (got {value!r})"
        ) from None
    if not math.isfinite(number):
        raise UsageError(f"--humidity needs a number like 68 (got {value!r})")
    if number != int(number):
        raise UsageError(
            f"--humidity needs a whole number of percent (got {value!r})"
        )
    humidity = int(number)
    if not (HUMIDITY_MIN <= humidity <= HUMIDITY_MAX):
        raise UsageError(
            "--humidity must be between {low} and {high} percent (got {seen})".format(
                low=HUMIDITY_MIN, high=HUMIDITY_MAX, seen=humidity
            )
        )
    return humidity


def parse_due_date(raw: Optional[str]) -> date:
    value = require_text(raw, "--due", "YYYY-MM-DD")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise UsageError(
            f"--due needs a date in YYYY-MM-DD format (got {value!r})"
        )
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise UsageError(
            f"--due needs a real calendar date in YYYY-MM-DD format (got {value!r})"
        ) from None


def parse_identifier(raw: Optional[str], flag: str = "--id") -> int:
    value = require_text(raw, flag, "reminder id")
    if not re.fullmatch(r"\d+", value):
        raise UsageError(f"{flag} needs a whole number (got {value!r})")
    return int(value)


def reminder_status(due_date: str, today: date) -> str:
    try:
        due = date.fromisoformat(due_date)
    except ValueError:
        return "unknown due date"
    days = (due - today).days
    if days < 0:
        return "OVERDUE"
    if days == 0:
        return "due today"
    return f"in {days}d"


def describe_cellar_file(path: str) -> str:
    try:
        stat = os.stat(path)
    except OSError:
        return f"cellar file: {path} (not readable)"
    modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
    return "cellar file: {path} ({size} bytes, modified {when})".format(
        path=path, size=stat.st_size, when=display_time(iso_timestamp(modified))
    )


def print_bare_status() -> None:
    """Help-first landing for a bare `corkage`: where the cellar is, then usage."""

    print(f"Corkage {VERSION} - cork condition, cellar climate and soak/decant reminders")
    path = store_path()
    if os.path.exists(path):
        print(describe_cellar_file(path))
    else:
        print(f"cellar file: {path} (not created yet - run: corkage init)")
    print()


def cmd_init(args: argparse.Namespace) -> int:
    """Create the cellar file and set the one cellar passphrase."""

    path = store_path()
    if os.path.exists(path):
        raise StoreError(MSG_ALREADY_INITIALISED.format(path=path))

    passphrase = passphrase_from_request(args)
    if passphrase is None:
        passphrase = prompt_new_passphrase()
    elif len(passphrase) < MIN_PASSPHRASE:
        raise UsageError(MSG_PASSPHRASE_SHORT)

    salt_hex, hash_hex = make_passphrase_record(passphrase)
    store = CellarStore(created_at=iso_timestamp(utc_now()))
    store.passphrase_salt = salt_hex
    store.passphrase_hash = hash_hex
    save_store(path, store)
    print(f"cellar initialised at {path}")
    print(
        "passphrase stored as a salted PBKDF2-HMAC-SHA256 hash "
        f"({PBKDF2_ITERATIONS} iterations, {SALT_BYTES}-byte salt)"
    )
    return EXIT_OK


def prompt_new_passphrase() -> str:
    while True:
        first = prompt_passphrase(
            f"new cellar passphrase (at least {MIN_PASSPHRASE} characters): "
        )
        if len(first) < MIN_PASSPHRASE:
            print(MSG_PASSPHRASE_SHORT, file=sys.stderr)
            continue
        second = prompt_passphrase("repeat passphrase: ")
        if first != second:
            print(MSG_PASSPHRASE_MISMATCH, file=sys.stderr)
            continue
        return first


def cmd_cork_add(args: argparse.Namespace) -> int:
    """Log one bottle's cork condition and extraction note."""

    bottle = require_text(args.bottle, "--bottle", "bottle name")
    condition = require_choice(
        args.condition, "--condition", "cork condition", CONDITIONS
    )
    note = (args.note or "").strip()

    path, store = unlock_cellar(args)
    now = utc_now()
    entry = CorkEntry(
        id=next_id(store, "cork"),
        bottle=bottle,
        condition=condition,
        note=note,
        created_at=iso_timestamp(now),
    )
    store.corks.append(entry)
    print(f"logged cork #{entry.id} for {entry.bottle} ({entry.condition})")

    reminder, created = sync_auto_decant(store, entry, now)
    if created and reminder is not None:
        print(
            f"decant reminder #{reminder.id} for {reminder.bottle} "
            f"due {reminder.due_date} (auto: {entry.condition} cork)"
        )
    elif reminder is not None:
        print(
            f"decant reminder #{reminder.id} for {reminder.bottle} "
            f"is already pending (due {reminder.due_date})"
        )

    save_store(path, store)
    return EXIT_OK


def sync_auto_decant(
    store: CellarStore, entry: CorkEntry, now: datetime
) -> Tuple[Optional[Reminder], bool]:
    """Soaked or crumbly corks get one pending decant reminder, due in 30 days."""

    if entry.condition not in AUTO_DECANT_CONDITIONS:
        return None, False
    for reminder in store.reminders:
        if (
            reminder.kind == "decant"
            and reminder.bottle == entry.bottle
            and reminder.done_at is None
        ):
            return reminder, False
    reminder = Reminder(
        id=next_id(store, "reminder"),
        kind="decant",
        bottle=entry.bottle,
        due_date=(now.date() + timedelta(days=AUTO_DECANT_DAYS)).isoformat(),
        note=f"auto: {entry.condition} cork logged {entry.created_at[:10]}",
        created_at=iso_timestamp(now),
    )
    store.reminders.append(reminder)
    return reminder, True


def cmd_cork_list(args: argparse.Namespace) -> int:
    """Print the cork log, newest first."""

    _, store = unlock_cellar(args)
    if not store.corks:
        print(EMPTY_CORK)
        return EXIT_OK
    ordered = sorted(store.corks, key=lambda item: (item.created_at, item.id), reverse=True)
    rows = [
        [
            str(entry.id),
            clamp(entry.bottle, 30),
            entry.condition,
            clamp(entry.note, 56) if entry.note else "-",
            display_time(entry.created_at),
        ]
        for entry in ordered
    ]
    print(
        render_table(
            ["ID", "BOTTLE", "CONDITION", "EXTRACTION NOTE", "LOGGED (UTC)"], rows
        )
    )
    return EXIT_OK


def cmd_reading_add(args: argparse.Namespace) -> int:
    """Record a cellar temperature and humidity reading."""

    temp_c = parse_temperature(args.temp)
    humidity = parse_humidity(args.humidity)
    note = (args.note or "").strip()

    path, store = unlock_cellar(args)
    reading = Reading(
        id=next_id(store, "reading"),
        temp_c=temp_c,
        humidity_pct=humidity,
        note=note,
        recorded_at=iso_timestamp(utc_now()),
    )
    store.readings.append(reading)
    save_store(path, store)
    print(
        f"recorded reading #{reading.id}: {reading.temp_c:.1f} C, "
        f"{reading.humidity_pct}% humidity"
    )
    return EXIT_OK


def cmd_reading_list(args: argparse.Namespace) -> int:
    """Print the cellar readings, newest first."""

    _, store = unlock_cellar(args)
    if not store.readings:
        print(EMPTY_READING)
        return EXIT_OK
    ordered = sorted(
        store.readings, key=lambda item: (item.recorded_at, item.id), reverse=True
    )
    rows = [
        [
            str(reading.id),
            f"{reading.temp_c:.1f} C",
            f"{reading.humidity_pct}%",
            clamp(reading.note, 56) if reading.note else "-",
            display_time(reading.recorded_at),
        ]
        for reading in ordered
    ]
    print(render_table(["ID", "TEMP", "HUMIDITY", "NOTE", "RECORDED (UTC)"], rows))
    return EXIT_OK


def cmd_reminder_add(args: argparse.Namespace) -> int:
    """Add a manual soak or decant reminder with a validated ISO due date."""

    kind = require_choice(args.kind, "--kind", "reminder kind", REMINDER_KINDS)
    due = parse_due_date(args.due)
    bottle = require_text(args.bottle, "--bottle", "bottle name")
    note = (args.note or "").strip()

    path, store = unlock_cellar(args)
    reminder = Reminder(
        id=next_id(store, "reminder"),
        kind=kind,
        bottle=bottle,
        due_date=due.isoformat(),
        note=note,
        created_at=iso_timestamp(utc_now()),
    )
    store.reminders.append(reminder)
    save_store(path, store)
    print(
        f"added {kind} reminder #{reminder.id} for {reminder.bottle} "
        f"due {reminder.due_date}"
    )
    return EXIT_OK


def cmd_reminder_list(args: argparse.Namespace) -> int:
    """Print every pending reminder, most urgent first."""

    _, store = unlock_cellar(args)
    pending = [reminder for reminder in store.reminders if reminder.done_at is None]
    if not pending:
        print(EMPTY_REMINDER)
        return EXIT_OK
    pending.sort(key=lambda item: (item.due_date, item.id))
    today = utc_now().date()
    rows = [
        [
            str(reminder.id),
            reminder.kind,
            clamp(reminder.bottle, 26),
            reminder.due_date,
            reminder_status(reminder.due_date, today),
            clamp(reminder.note, 44) if reminder.note else "-",
        ]
        for reminder in pending
    ]
    print(
        render_table(
            ["ID", "KIND", "BOTTLE", "DUE", "STATUS", "NOTE"],
            rows,
            highlight=("OVERDUE",),
        )
    )
    return EXIT_OK


def cmd_reminder_done(args: argparse.Namespace) -> int:
    """Mark a pending reminder done so it leaves the pending list."""

    reminder_id = parse_identifier(args.id)
    path, store = unlock_cellar(args)
    target = next(
        (
            reminder
            for reminder in store.reminders
            if reminder.id == reminder_id and reminder.done_at is None
        ),
        None,
    )
    if target is None:
        raise UsageError(f"no pending reminder with id {reminder_id}")
    target.done_at = iso_timestamp(utc_now())
    save_store(path, store)
    print(
        f"reminder #{target.id} marked done "
        f"({target.kind} {target.bottle}, was due {target.due_date})"
    )
    return EXIT_OK


# --- demo cellar -------------------------------------------------------------
# Sample rows a keeper would really write: real bottle names, dates over the
# last few weeks, mixed conditions, two edge readings.  Dates are relative to
# now so the demo never looks stale.

SAMPLE_CORKS: Tuple[Tuple[int, int, str, str, str], ...] = (
    (12, 19, "Barolo 2016", "soaked", "stained two thirds up, slid out clean"),
    (21, 10, "Rioja Reserva 2012", "sound", "snug fit, firm and dry, no staining"),
    (9, 20, "Vouvray Sec 2018", "crumbly", "broke into three pieces at the base"),
    (34, 18, "Chianti Classico 2015", "sound", "clean pull, faint ullage"),
    (5, 21, "Sauternes 2009", "moldy", "grey mould on the top disc, wine smelled clean"),
    (2, 12, "Vintage Port 1998", "soaked", "saturated to the top, worm went in easily"),
    (27, 9, "Barbera d'Asti 2017", "crumbly", "dry and brittle, crumbled under the worm"),
)

SAMPLE_READINGS: Tuple[Tuple[int, int, float, int, str], ...] = (
    (2, 18, 15.0, 74, "door left open during bottling"),
    (6, 9, 13.5, 68, "after racking"),
    (13, 20, 14.2, 71, "warm spell upstairs, cellar held steady"),
    (20, 7, 12.8, 63, "cold snap overnight"),
    (27, 21, 16.8, 79, "heatwave, added a fan"),
    (34, 8, 13.1, 66, ""),
    (41, 17, 12.4, 61, "bottled the Syrah, cellar door open 40 minutes"),
    (55, 11, 11.9, 59, ""),
)

SAMPLE_REMINDERS: Tuple[Tuple[int, str, str, str], ...] = (
    (5, "soak", "Zinfandel 2014", "dry cork from a cold cellar, soak overnight"),
    (-3, "decant", "Rioja Reserva 2012", "sediment at the shoulder, decant an hour before"),
    (-10, "soak", "Syrah 2013", "three months in the cellar, cork is dry"),
    (0, "decant", "Chianti Classico 2015", "open for the tasting tonight"),
)


def seed_demo_store(store: CellarStore, now: datetime) -> Dict[str, int]:
    """Idempotently add the sample corks, readings and reminders."""

    if any(entry.bottle == SAMPLE_CORKS[0][2] for entry in store.corks):
        return {"corks": 0, "readings": 0, "reminders": 0}

    added = {"corks": 0, "readings": 0, "reminders": 0}
    if not store.created_at:
        store.created_at = iso_timestamp(now)

    for days_ago, hour, bottle, condition, note in sorted(
        SAMPLE_CORKS, key=lambda item: item[0], reverse=True
    ):
        entry = CorkEntry(
            id=next_id(store, "cork"),
            bottle=bottle,
            condition=condition,
            note=note,
            created_at=iso_timestamp(_moment(now, days_ago, hour)),
        )
        store.corks.append(entry)
        added["corks"] += 1
        sync_auto_decant(store, entry, _moment(now, days_ago, hour))
    store.corks.sort(key=lambda item: (item.created_at, item.id))

    for days_ago, hour, temp_c, humidity, note in sorted(
        SAMPLE_READINGS, key=lambda item: item[0], reverse=True
    ):
        store.readings.append(
            Reading(
                id=next_id(store, "reading"),
                temp_c=temp_c,
                humidity_pct=humidity,
                note=note,
                recorded_at=iso_timestamp(_moment(now, days_ago, hour)),
            )
        )
        added["readings"] += 1
    store.readings.sort(key=lambda item: (item.recorded_at, item.id))

    for offset_days, kind, bottle, note in SAMPLE_REMINDERS:
        store.reminders.append(
            Reminder(
                id=next_id(store, "reminder"),
                kind=kind,
                bottle=bottle,
                due_date=(now.date() + timedelta(days=offset_days)).isoformat(),
                note=note,
                created_at=iso_timestamp(now),
            )
        )
        added["reminders"] += 1
    return added


def _moment(now: datetime, days_ago: int, hour: int) -> datetime:
    """A plausible past moment: same clock hour, N days back."""

    stamp = (now - timedelta(days=days_ago)).replace(
        hour=hour, minute=(hour * 7) % 60, second=0, microsecond=0
    )
    return stamp


def cmd_demo(args: argparse.Namespace) -> int:
    """Create or refresh the preview cellar (passphrase: cenius) with sample rows."""

    path = store_path()
    existing = os.path.exists(path)
    store = load_store(path) if existing else CellarStore()
    if existing and not verify_passphrase(store, DEMO_PASSPHRASE):
        print(
            f"a cellar already exists at {path} and it is not the demo cellar "
            "- leaving it untouched",
            file=sys.stderr,
        )
        print(
            "hint: point CORKAGE_HOME at an empty directory to seed a demo cellar there",
            file=sys.stderr,
        )
        return EXIT_OK

    if args.reset or not existing:
        store = CellarStore()
        store.passphrase_salt, store.passphrase_hash = make_passphrase_record(
            DEMO_PASSPHRASE
        )
    now = utc_now()
    added = seed_demo_store(store, now)
    if any(added.values()) or args.reset or not existing:
        save_store(path, store)

    pending = [item for item in store.reminders if item.done_at is None]
    print(f"demo cellar ready at {path}")
    print(f"  passphrase: {DEMO_PASSPHRASE}   (the standard preview credential)")
    print(
        f"  corks: {len(store.corks)}   readings: {len(store.readings)}   "
        f"pending reminders: {len(pending)}"
    )
    if not any(added.values()):
        print("  (already seeded - nothing was added)")
    print("try: corkage cork list   or: corkage reminder list")
    return EXIT_OK


EPILOG = """\
exit codes:
  0  ok
  1  unexpected error
  2  usage or validation error
  3  cellar unlock or store error

examples:
  corkage init
  corkage cork add --bottle "Barolo 2016" --condition soaked --note "stained halfway up"
  corkage cork list
  corkage reading add --temp 13.5 --humidity 68 --note "after racking"
  corkage reading list
  corkage reminder add --kind soak --due 2027-06-01 --bottle "Rioja 2012"
  corkage reminder list
  corkage reminder done --id 2
  corkage demo

environment:
  CORKAGE_HOME        directory holding store.json (default: ~/.corkage)
  CORKAGE_PASSPHRASE  cellar passphrase, avoids the hidden prompt
  CORKAGE_COLOR       always or never, overrides the TTY colour check
  NO_COLOR            set to disable the accent colour in tables
"""

PASSPHRASE_HELP = (
    "cellar passphrase; falls back to $CORKAGE_PASSPHRASE, then a hidden prompt"
)


def _add_passphrase(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--passphrase", metavar="TEXT", help=PASSPHRASE_HELP)


def build_parser() -> Tuple[argparse.ArgumentParser, Dict[str, argparse.ArgumentParser]]:
    """Build the three-level command surface and remember every leaf parser."""

    top = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Corkage logs each bottle's cork condition and extraction note, tracks "
            "cellar temperature and humidity, and keeps soak and decant reminders."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    top.add_argument("--version", action="version", version=f"{PROG} {VERSION}")
    commands = top.add_subparsers(dest="command", metavar="COMMAND")
    parsers: Dict[str, argparse.ArgumentParser] = {"": top}

    init_parser = commands.add_parser(
        "init",
        help="create the cellar file and set the cellar passphrase",
        description=(
            "Create $CORKAGE_HOME/store.json and set the one cellar passphrase. "
            "The passphrase is prompted twice and stored only as a salted "
            "PBKDF2-HMAC-SHA256 hash."
        ),
        allow_abbrev=False,
    )
    _add_passphrase(init_parser)
    init_parser.set_defaults(handler=cmd_init)
    parsers["init"] = init_parser

    cork_parser = commands.add_parser(
        "cork",
        help="log a bottle's cork condition and read the cork log",
        description="Cork log: what the cork looked like and how it came out.",
        allow_abbrev=False,
    )
    parsers["cork"] = cork_parser
    cork_actions = cork_parser.add_subparsers(dest="action", metavar="ACTION")

    cork_add = cork_actions.add_parser(
        "add",
        help="log one bottle's cork condition and extraction note",
        description=(
            "Log a cork. A soaked or crumbly cork also queues one pending decant "
            "reminder due 30 days from today; sound and moldy corks queue nothing."
        ),
        allow_abbrev=False,
    )
    cork_add.add_argument("--bottle", metavar="NAME", help="bottle, e.g. \"Barolo 2016\"")
    cork_add.add_argument(
        "--condition",
        metavar="COND",
        help="cork condition: sound, soaked, crumbly or moldy (case-insensitive)",
    )
    cork_add.add_argument("--note", metavar="TEXT", help="extraction note")
    _add_passphrase(cork_add)
    cork_add.set_defaults(handler=cmd_cork_add)
    parsers["cork add"] = cork_add

    cork_list = cork_actions.add_parser(
        "list",
        help="print the cork log, newest first",
        description="Print every cork entry with bottle, condition, note and time.",
        allow_abbrev=False,
    )
    _add_passphrase(cork_list)
    cork_list.set_defaults(handler=cmd_cork_list)
    parsers["cork list"] = cork_list

    reading_parser = commands.add_parser(
        "reading",
        help="record cellar temperature and humidity, or read the log",
        description="Cellar climate log in Celsius and percent relative humidity.",
        allow_abbrev=False,
    )
    parsers["reading"] = reading_parser
    reading_actions = reading_parser.add_subparsers(dest="action", metavar="ACTION")

    reading_add = reading_actions.add_parser(
        "add",
        help="record a cellar temperature and humidity reading",
        description=(
            "Record a reading. --temp accepts one decimal in Celsius (-5 to 45); "
            "--humidity is a whole percent (0 to 100)."
        ),
        allow_abbrev=False,
    )
    reading_add.add_argument("--temp", metavar="C", help="temperature in Celsius, e.g. 13.5")
    reading_add.add_argument("--humidity", metavar="PCT", help="relative humidity percent, e.g. 68")
    reading_add.add_argument("--note", metavar="TEXT", help="optional note")
    _add_passphrase(reading_add)
    reading_add.set_defaults(handler=cmd_reading_add)
    parsers["reading add"] = reading_add

    reading_list = reading_actions.add_parser(
        "list",
        help="print the cellar readings, newest first",
        description="Print every reading with temperature, humidity, note and time.",
        allow_abbrev=False,
    )
    _add_passphrase(reading_list)
    reading_list.set_defaults(handler=cmd_reading_list)
    parsers["reading list"] = reading_list

    reminder_parser = commands.add_parser(
        "reminder",
        help="keep soak and decant reminders",
        description="Soak and decant reminders, manual or automatic, with due dates.",
        allow_abbrev=False,
    )
    parsers["reminder"] = reminder_parser
    reminder_actions = reminder_parser.add_subparsers(dest="action", metavar="ACTION")

    reminder_add = reminder_actions.add_parser(
        "add",
        help="add a soak or decant reminder with an ISO due date",
        description="Add a reminder. --due is an ISO calendar date, YYYY-MM-DD.",
        allow_abbrev=False,
    )
    reminder_add.add_argument("--kind", metavar="KIND", help="reminder kind: soak or decant")
    reminder_add.add_argument("--due", metavar="YYYY-MM-DD", help="ISO due date, YYYY-MM-DD")
    reminder_add.add_argument("--bottle", metavar="NAME", help="bottle the reminder is for")
    reminder_add.add_argument("--note", metavar="TEXT", help="optional note")
    _add_passphrase(reminder_add)
    reminder_add.set_defaults(handler=cmd_reminder_add)
    parsers["reminder add"] = reminder_add

    reminder_list = reminder_actions.add_parser(
        "list",
        help="print pending reminders, most urgent first",
        description="Print every pending reminder, with OVERDUE on past due dates.",
        allow_abbrev=False,
    )
    _add_passphrase(reminder_list)
    reminder_list.set_defaults(handler=cmd_reminder_list)
    parsers["reminder list"] = reminder_list

    reminder_done = reminder_actions.add_parser(
        "done",
        help="mark a pending reminder done",
        description="Mark a pending reminder done so it leaves the pending list.",
        allow_abbrev=False,
    )
    reminder_done.add_argument("--id", metavar="N", help="id of the pending reminder")
    _add_passphrase(reminder_done)
    reminder_done.set_defaults(handler=cmd_reminder_done)
    parsers["reminder done"] = reminder_done

    demo_parser = commands.add_parser(
        "demo",
        help="create or refresh the demo cellar with sample corks, readings and reminders",
        description=(
            "Create a demo cellar with sample data and the standard preview "
            "passphrase 'cenius'. Idempotent, and it never overwrites a cellar "
            "that uses a different passphrase."
        ),
        allow_abbrev=False,
    )
    demo_parser.add_argument(
        "--reset", action="store_true", help="rebuild the demo cellar from scratch"
    )
    demo_parser.set_defaults(handler=cmd_demo)
    parsers["demo"] = demo_parser

    return top, parsers


def resolve_leaf(
    args: argparse.Namespace, parsers: Dict[str, argparse.ArgumentParser]
) -> Tuple[str, Optional[argparse.ArgumentParser]]:
    command = args.command or ""
    action = getattr(args, "action", None)
    key = f"{command} {action}" if action else command
    candidate = parsers.get(key)
    if candidate is not None and callable(candidate.get_default("handler")):
        return key, candidate
    return key, None


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse, dispatch, and map every failure onto the documented exit code."""

    global _ACTIVE_USAGE
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser, parsers = build_parser()

    if not arguments:
        print_bare_status()
        parser.print_help()
        return EXIT_OK

    try:
        args, extra = parser.parse_known_args(arguments)
    except SystemExit as exc:
        # argparse has already printed usage or help to stderr/stdout.
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE

    try:
        _, leaf = resolve_leaf(args, parsers)
        if leaf is not None:
            _ACTIVE_USAGE = leaf.format_usage().strip()
        else:
            group = parsers.get(args.command or "")
            _ACTIVE_USAGE = (group or parser).format_usage().strip()

        if extra:
            raise UsageError(
                "unrecognized arguments: " + " ".join(extra), show_usage=True
            )
        if leaf is None:
            actions = GROUP_ACTIONS.get(args.command or "")
            if actions:
                raise UsageError(
                    f"missing {args.command} action - choose one of: "
                    f"{', '.join(actions)}",
                    show_usage=True,
                )
            raise UsageError("no command given - run corkage --help", show_usage=True)

        handler = leaf.get_default("handler")
        if not callable(handler):
            raise UsageError("no command given - run corkage --help", show_usage=True)
        return handler(args)
    except UsageError as exc:
        emit_error(exc.message, _ACTIVE_USAGE if exc.show_usage else None)
        return EXIT_USAGE
    except StoreError as exc:
        emit_error(exc.message)
        return EXIT_STORE
    except KeyboardInterrupt:
        emit_error("interrupted")
        return 130
    except BrokenPipeError:
        return EXIT_OK
    except Exception as exc:  # process boundary: never show a traceback to the user
        if os.environ.get(ENV_DEBUG):
            traceback.print_exc()
        emit_error(f"unexpected error: {exc.__class__.__name__}: {exc}")
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.exit(main())
