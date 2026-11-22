#!/usr/bin/env python3
"""Polite, resumable Scryfall bulk-data tracker and card-image downloader.

Subcommands:
  track     Refresh/read Oracle Cards bulk data and record matching images.
  download  Download pending images or extra URL-ID lists.
  favorite  Copy downloaded cards into deck-like folders from text lists.
  status    Print database and cache progress.

Python 3.10+, standard library only. State lives in state.sqlite3; interrupting
and rerunning a command is safe.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import gzip
import http.client
import json
import random
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# This query documents the filter below; this program does not evaluate arbitrary
# Scryfall syntax locally. The date is Shadows over Innistrad's release date.
SEARCH_QUERY = "date>=soi (c:c or c>=w or c>=g) (r:uncommon or r:rare or r:mythic)"
START_DATE = "2016-04-08"
BULK_DATA_TYPE = "oracle_cards"
BULK_INFO_URL = "https://api.scryfall.com/bulk-data"
DEFAULT_DOWNLOAD_DIR = "_download"
LEGACY_DOWNLOAD_DIR = "scryfall_cards"
FAVORITE_OUTPUT_DIR = "_favorite"
DEFAULT_FAVORITE_FILE = "favorite.txt"
IMAGE_FORMAT = "png"
ONLY_FRONT_FACE = True
IMAGE_EXTENSIONS = {
    "png": "png",
    "border_crop": "png",
    "art_crop": "jpg",
    "large": "jpg",
    "normal": "jpg",
    "small": "jpg",
}

# api.scryfall.com allows 10 requests/second on the endpoints used here. Keep a
# 100-150 ms gap even for file origins so a run remains predictably gentle.
UA = "scryfall-watcher/1.0 (personal card image archival)"
DELAY = 0.10
JITTER = 0.05
MAX_RETRIES = 5
BACKOFF = (1, 2, 4, 8, 30)
HEADERS = {"User-Agent": UA, "Accept": "application/json"}


def log(message: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {message}", flush=True)


def polite_sleep() -> None:
    time.sleep(DELAY + random.uniform(0, JITTER))


def retry_after(error: urllib.error.HTTPError, default: int) -> int:
    raw = error.headers.get("Retry-After", "")
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(raw)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=dt.timezone.utc)
                return max(0, int((when - dt.datetime.now(dt.timezone.utc)).total_seconds()))
            except (TypeError, ValueError):
                pass
    return max(30, default) if error.code == 429 else default


def fetch_bytes(url: str) -> bytes:
    """GET a small API/JSON resource with retries and Retry-After support."""
    for attempt in range(MAX_RETRIES):
        polite_sleep()
        request = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == MAX_RETRIES - 1:
                raise RuntimeError(f"HTTP {error.code}: {url}") from error
            wait = retry_after(error, BACKOFF[min(attempt, len(BACKOFF) - 1)])
            log(f"HTTP {error.code}; retrying in {wait}s")
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as error:
            if attempt == MAX_RETRIES - 1:
                raise RuntimeError(f"{type(error).__name__}: {url}") from error
            wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            log(f"{type(error).__name__}; retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"unreachable: {url}")


def fetch_json(url: str) -> dict[str, Any]:
    value = json.loads(fetch_bytes(url).decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected a JSON object: {url}")
    return value


def download_file(url: str, destination: Path) -> None:
    """Download to .part, resume with Range when possible, then atomically replace."""
    part = destination.with_name(destination.name + ".part")
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(MAX_RETRIES):
        position = part.stat().st_size if part.exists() else 0
        headers = dict(HEADERS)
        if position:
            headers["Range"] = f"bytes={position}-"
        polite_sleep()
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                # A server may ignore Range and return 200. In that case, restart
                # from byte zero using the response already opened.
                append = bool(position and response.status == 206)
                with part.open("ab" if append else "wb") as output:
                    shutil.copyfileobj(response, output, 1024 * 256)
            if part.stat().st_size == 0:
                raise RuntimeError(f"empty response: {url}")
            part.replace(destination)
            return
        except urllib.error.HTTPError as error:
            if error.code == 416 and part.exists() and attempt < MAX_RETRIES - 1:
                log(f"HTTP 416; discarding stale partial file for {destination.name}")
                part.unlink(missing_ok=True)
                continue
            if error.code not in (429, 500, 502, 503, 504) or attempt == MAX_RETRIES - 1:
                raise RuntimeError(f"HTTP {error.code}: {url}") from error
            wait = retry_after(error, BACKOFF[min(attempt, len(BACKOFF) - 1)])
            log(f"HTTP {error.code}; retrying {destination.name} in {wait}s")
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as error:
            if attempt == MAX_RETRIES - 1:
                raise RuntimeError(f"{type(error).__name__}: {url}") from error
            wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            log(f"{type(error).__name__}; retrying {destination.name} in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"failed after {MAX_RETRIES} attempts: {url}")


_WIN_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_component(value: str, cap: int = 120) -> str:
    value = _WIN_BAD.sub("_", value)
    value = re.sub(r"\s+", " ", value).strip().rstrip(" .")
    return (value[:cap].rstrip(" .") or "unknown")


SCHEMA = """
CREATE TABLE IF NOT EXISTS cards(
  id TEXT PRIMARY KEY,
  oracle_id TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL,
  released_at TEXT NOT NULL DEFAULT '',
  rarity TEXT NOT NULL DEFAULT '',
  set_code TEXT NOT NULL DEFAULT '',
  collector_number TEXT NOT NULL DEFAULT '',
  scryfall_uri TEXT NOT NULL DEFAULT '',
  layout TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS images(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
  face_index INTEGER NOT NULL DEFAULT 0,
  face_name TEXT NOT NULL,
  image_format TEXT NOT NULL,
  url TEXT NOT NULL,
  local_name TEXT NOT NULL DEFAULT '',
  source_path TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'new',  -- new | done | failed
  error TEXT NOT NULL DEFAULT '',
  UNIQUE(card_id, face_index, image_format)
);
CREATE INDEX IF NOT EXISTS idx_images_status ON images(status, id);
CREATE TABLE IF NOT EXISTS extra_images(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  list_key TEXT NOT NULL,              -- output directory below _download/
  entry TEXT NOT NULL,                 -- original URL ID from the text list
  card_id TEXT NOT NULL DEFAULT '',
  face_index INTEGER NOT NULL DEFAULT 0,
  face_name TEXT NOT NULL DEFAULT '',
  image_format TEXT NOT NULL,
  url TEXT NOT NULL DEFAULT '',
  local_name TEXT NOT NULL DEFAULT '',
  source_path TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'new',  -- new | done | failed
  error TEXT NOT NULL DEFAULT '',
  UNIQUE(list_key, entry, face_index, image_format)
);
CREATE INDEX IF NOT EXISTS idx_extra_status ON extra_images(status, id);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


class DB:
    def __init__(self, base: Path):
        base.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(base / "state.sqlite3")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        # Databases created by version 1.0 did not record the actual image path.
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(images)")}
        if "source_path" not in columns:
            self.conn.execute("ALTER TABLE images ADD COLUMN source_path TEXT NOT NULL DEFAULT ''")

    def __enter__(self) -> "DB":
        return self

    def __exit__(self, *args: object) -> None:
        self.conn.commit()
        self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    def get_kv(self, key: str) -> str:
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return row[0] if row else ""

    def set_kv(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, value),
        )


# ------------------------------------------------------------------- track ----


def iter_bulk(path: Path) -> Iterator[dict[str, Any]]:
    """Stream current JSONL.GZ bulk files or read a legacy JSON-array cache."""
    log(f"loading bulk data: {path}")
    try:
        if path.suffix.lower() == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as source:
                for line_number, line in enumerate(source, 1):
                    if not line.strip():
                        continue
                    try:
                        card = json.loads(line)
                    except json.JSONDecodeError as error:
                        raise RuntimeError(f"invalid JSONL at {path}:{line_number}: {error}") from error
                    if not isinstance(card, dict):
                        raise RuntimeError(f"expected a card object at {path}:{line_number}")
                    yield card
        else:
            with path.open("r", encoding="utf-8") as source:
                data = json.load(source)
            if not isinstance(data, list):
                raise RuntimeError(f"legacy bulk data is not a JSON array: {path}")
            for card in data:
                if isinstance(card, dict):
                    yield card
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read bulk data {path}: {error}") from error


def prepare_bulk(db: DB, base: Path, update: bool, offline: bool) -> tuple[Path, dict[str, str]]:
    cache = base / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    legacy_path = cache / "bulk_data.json"
    jsonl_path = cache / "bulk_data.jsonl.gz"

    if offline:
        bulk_path = jsonl_path if jsonl_path.exists() else legacy_path
        if not bulk_path.exists():
            raise RuntimeError(
                "offline mode requires cache/bulk_data.jsonl.gz or cache/bulk_data.json; "
                "run track online first"
            )
        return bulk_path, {}

    response = fetch_json(BULK_INFO_URL)
    if response.get("object") == "error":
        raise RuntimeError(f"Scryfall bulk-data error: {response.get('details') or response}")
    items = response.get("data")
    if isinstance(items, list):
        info = next((item for item in items if item.get("type") == BULK_DATA_TYPE), None)
    elif response.get("type") == BULK_DATA_TYPE:
        info = response  # Also accept a single bulk_data object for compatibility.
    else:
        info = None
    if not isinstance(info, dict):
        raise RuntimeError(f"Scryfall bulk-data response has no {BULK_DATA_TYPE} entry")

    jsonl_uri = str(info.get("jsonl_download_uri") or "")
    legacy_uri = str(info.get("download_uri") or "")
    download_uri = jsonl_uri or legacy_uri
    updated_at = str(info.get("updated_at") or "")
    if not download_uri:
        raise RuntimeError(f"Scryfall {BULK_DATA_TYPE} entry has no supported download URI: {info}")
    bulk_path = jsonl_path if jsonl_uri else legacy_path
    version = f"{updated_at}|{download_uri}"
    current = bulk_path.exists() and not update and db.get_kv("bulk_version") == version

    if current:
        log(f"bulk data unchanged ({updated_at or 'unknown update time'}); using cache")
    else:
        size = info.get("compressed_size")
        size_text = f", {int(size) / 1e6:.1f} MB" if isinstance(size, int) else ""
        log(f"downloading {BULK_DATA_TYPE} bulk data ({updated_at or 'unknown'}{size_text})")
        download_file(download_uri, bulk_path)
    return bulk_path, {
        "version": version,
        "updated_at": updated_at,
        "download_uri": download_uri,
    }


def matches_filter(card: dict[str, Any]) -> bool:
    """Preserve the old script's local interpretation of SEARCH_QUERY."""
    if card.get("rarity") not in ("uncommon", "rare", "mythic"):
        return False
    colors = card.get("colors") or []
    identity = card.get("color_identity") or []
    if not ("C" in colors or not colors or "W" in identity or "G" in identity):
        return False
    return bool(card.get("released_at", "") >= START_DATE)


def image_specs(
    card: dict[str, Any], image_format: str, only_front: bool
) -> list[tuple[int, str, str, str]]:
    """Return (face index, face name, image URL, filename suffix) tuples."""
    card_name = str(card.get("name") or "unknown")
    top_uris = card.get("image_uris")
    if isinstance(top_uris, dict) and top_uris.get(image_format):
        return [(0, card_name, str(top_uris[image_format]), "")]

    faces = card.get("card_faces")
    if not isinstance(faces, list):
        return []
    specs: list[tuple[int, str, str, str]] = []
    for index, face in enumerate(faces):
        if not isinstance(face, dict):
            continue
        uris = face.get("image_uris")
        if not isinstance(uris, dict) or not uris.get(image_format):
            continue
        suffix = f"_face{index + 1}" if len(faces) > 1 else ""
        specs.append((index, str(face.get("name") or card_name), str(uris[image_format]), suffix))
        if only_front:
            break
    return specs


def image_filename(
    card: dict[str, Any], face_name: str, suffix: str, image_format: str
) -> str:
    face = safe_component(face_name, 120)
    set_code = safe_component(str(card.get("set") or "unknown"), 24)
    number = safe_component(str(card.get("collector_number") or "0"), 32)
    extension = IMAGE_EXTENSIONS[image_format]
    return f"{face}_{set_code}_{number}{suffix}.{extension}"


def upsert_card(db: DB, card: dict[str, Any]) -> str:
    card_id = str(card.get("id") or "")
    if not card_id:
        raise RuntimeError("card object has no id")
    db.conn.execute(
        "INSERT INTO cards(id,oracle_id,name,released_at,rarity,set_code,"
        "collector_number,scryfall_uri,layout) VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET oracle_id=excluded.oracle_id,"
        "name=excluded.name,released_at=excluded.released_at,rarity=excluded.rarity,"
        "set_code=excluded.set_code,collector_number=excluded.collector_number,"
        "scryfall_uri=excluded.scryfall_uri,layout=excluded.layout",
        (
            card_id,
            str(card.get("oracle_id") or ""),
            str(card.get("name") or "unknown"),
            str(card.get("released_at") or ""),
            str(card.get("rarity") or ""),
            str(card.get("set") or ""),
            str(card.get("collector_number") or ""),
            str(card.get("scryfall_uri") or ""),
            str(card.get("layout") or ""),
        ),
    )
    return card_id


def input_path(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


def read_entries(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as error:
        raise RuntimeError(f"cannot read list {path}: {error}") from error
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def collect_lists(
    base: Path, files: list[str] | None, directories: list[str] | None
) -> list[tuple[Path, str]]:
    """Return (text file, safe relative output key) pairs in stable order."""
    collected: list[tuple[Path, str]] = []
    for value in files or []:
        path = input_path(base, value)
        if not path.is_file():
            raise RuntimeError(f"list file not found: {path}")
        collected.append((path, safe_component(path.stem)))
    for value in directories or []:
        root = input_path(base, value)
        if not root.is_dir():
            raise RuntimeError(f"list directory not found: {root}")
        for path in sorted(root.rglob("*.txt")):
            if not path.is_file():
                continue
            relative = path.relative_to(root).with_suffix("")
            parts = [safe_component(root.name)]
            parts.extend(safe_component(part) for part in relative.parts)
            collected.append((path, "/".join(parts)))
    unique: list[tuple[Path, str]] = []
    seen: set[tuple[str, str]] = set()
    for path, key in collected:
        identity = (str(path.resolve()), key)
        if identity not in seen:
            seen.add(identity)
            unique.append((path, key))
    return unique


def cmd_track(db: DB, args: argparse.Namespace) -> None:
    if args.offline and args.update:
        raise SystemExit("--offline and --update cannot be used together")
    base = Path(args.base)
    bulk_path, bulk_meta = prepare_bulk(db, base, args.update, args.offline)
    all_cards = iter_bulk(bulk_path)
    log(f"filter: {SEARCH_QUERY}")
    log(f"image format: {args.image_format}; only front face: {not args.all_faces}")

    matched = new_cards = new_images = without_images = 0
    for card in all_cards:
        if not isinstance(card, dict) or not matches_filter(card):
            continue
        card_id = str(card.get("id") or "")
        if not card_id:
            continue
        matched += 1
        exists = db.conn.execute("SELECT 1 FROM cards WHERE id=?", (card_id,)).fetchone()
        if not exists:
            new_cards += 1
        upsert_card(db, card)
        specs = image_specs(card, args.image_format, not args.all_faces)
        if not specs:
            without_images += 1
        for face_index, face_name, url, suffix in specs:
            local_name = image_filename(card, face_name, suffix, args.image_format)
            key = (card_id, face_index, args.image_format)
            old = db.conn.execute(
                "SELECT 1 FROM images WHERE card_id=? AND face_index=? AND image_format=?",
                key,
            ).fetchone()
            if not old:
                new_images += 1
            db.conn.execute(
                "INSERT INTO images(card_id,face_index,face_name,image_format,url,local_name) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(card_id,face_index,image_format) DO UPDATE SET "
                "face_name=excluded.face_name,url=excluded.url,"
                "local_name=CASE WHEN images.url<>excluded.url OR images.local_name='' "
                "THEN excluded.local_name ELSE images.local_name END,"
                "status=CASE WHEN images.url<>excluded.url THEN 'new' ELSE images.status END,"
                "error=CASE WHEN images.url<>excluded.url THEN '' ELSE images.error END",
                (card_id, face_index, face_name, args.image_format, url, local_name),
            )
        if matched % 500 == 0:
            db.commit()
            log(f"processed {matched} matching cards")
        if args.max_cards is not None and matched >= args.max_cards:
            log(f"--max-cards {args.max_cards} reached")
            break

    if bulk_meta:
        db.set_kv("bulk_version", bulk_meta["version"])
        db.set_kv("bulk_updated_at", bulk_meta["updated_at"])
        db.set_kv("bulk_download_uri", bulk_meta["download_uri"])
    db.set_kv("image_format", args.image_format)
    db.set_kv("only_front_face", str(not args.all_faces))
    db.commit()
    log(
        f"track done: {matched} matched, {new_cards} new cards, "
        f"{new_images} new images, {without_images} without requested image"
    )


# ---------------------------------------------------------------- download ----


def parse_url_id(value: str) -> tuple[str, str] | None:
    """Parse set-number-name, set/number/name, or a Scryfall card URL."""
    text = value.strip()
    parts: list[str]
    if text.startswith(("https://", "http://")):
        parsed = urllib.parse.urlparse(text)
        parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
        if "card" not in parts:
            return None
        parts = parts[parts.index("card") + 1 :]
    elif "/" in text:
        parts = [part for part in text.strip("/").split("/") if part]
    else:
        parts = text.split("-")
        # A bare card name containing spaces must not be mistaken for a URL ID.
        if len(parts) < 2 or (len(parts) == 2 and not parts[1].isdigit()):
            return None
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


def fetch_card_by_url_id(value: str) -> dict[str, Any]:
    parsed = parse_url_id(value)
    if not parsed:
        raise RuntimeError(f"invalid Scryfall URL ID: {value}")
    set_code, number = (urllib.parse.quote(part, safe="") for part in parsed)
    card = fetch_json(f"https://api.scryfall.com/cards/{set_code}/{number}")
    if card.get("object") == "error":
        raise RuntimeError(str(card.get("details") or f"card not found: {value}"))
    return card


def record_extra_error(db: DB, list_key: str, entry: str, image_format: str, error: Exception) -> None:
    db.conn.execute(
        "INSERT INTO extra_images(list_key,entry,image_format,status,error) VALUES(?,?,?,'failed',?) "
        "ON CONFLICT(list_key,entry,face_index,image_format) DO UPDATE SET "
        "status='failed',error=excluded.error",
        (list_key, entry, image_format, str(error)[:400]),
    )
    db.commit()
    log(f"FAILED to resolve {entry}: {error}")


def register_extra_lists(
    db: DB, lists: list[tuple[Path, str]], download_root: Path
) -> list[str]:
    image_format = db.get_kv("image_format") or IMAGE_FORMAT
    keys: list[str] = []
    for path, list_key in lists:
        keys.append(list_key)
        log(f"include list: {path} -> {download_root / Path(*list_key.split('/'))}")
        for entry in read_entries(path):
            old = db.conn.execute(
                "SELECT url FROM extra_images WHERE list_key=? AND entry=? "
                "AND face_index=0 AND image_format=?",
                (list_key, entry, image_format),
            ).fetchone()
            if old and old[0]:
                continue  # Resolution is cached; only the file download needs retrying.
            try:
                log(f"resolve {entry}")
                card = fetch_card_by_url_id(entry)
                card_id = upsert_card(db, card)
                specs = image_specs(card, image_format, only_front=True)
                if not specs:
                    raise RuntimeError(f"no {image_format} front-face image for {entry}")
                face_index, face_name, url, suffix = specs[0]
                local_name = image_filename(card, face_name, suffix, image_format)
                db.conn.execute(
                    "INSERT INTO extra_images(list_key,entry,card_id,face_index,face_name,"
                    "image_format,url,local_name) VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(list_key,entry,face_index,image_format) DO UPDATE SET "
                    "card_id=excluded.card_id,face_name=excluded.face_name,url=excluded.url,"
                    "local_name=excluded.local_name,source_path=CASE WHEN "
                    "extra_images.url=excluded.url THEN extra_images.source_path ELSE '' END,"
                    "status=CASE WHEN extra_images.status='done' AND extra_images.url=excluded.url "
                    "THEN 'done' ELSE 'new' END,error=''",
                    (list_key, entry, card_id, face_index, face_name, image_format, url, local_name),
                )
                db.commit()
            except Exception as error:
                record_extra_error(db, list_key, entry, image_format, error)
    return keys


def path_owner_urls(db: DB, table: str, image_id: int, destination: Path) -> set[str]:
    """Return URLs already associated with a path by either download queue."""
    owners: set[str] = set()
    path = str(destination.resolve())
    for owner_table in ("images", "extra_images"):
        if owner_table == table:
            rows = db.conn.execute(
                f"SELECT url FROM {owner_table} WHERE source_path=? AND id<>?", (path, image_id)
            )
        else:
            rows = db.conn.execute(f"SELECT url FROM {owner_table} WHERE source_path=?", (path,))
        owners.update(row[0] for row in rows if row[0])
    return owners


def available_destination(db: DB, table: str, image_id: int, url: str, original: Path) -> Path:
    """Reuse an identical file, or allocate -2/-3 names for different files."""
    candidate = original
    number = 2
    resolving_collision = False
    while True:
        owners = path_owner_urls(db, table, image_id, candidate)
        if url in owners:
            return candidate
        if owners or (candidate.exists() and resolving_collision):
            # Another source owns this name. An unowned file later in a -2/-3
            # sequence is also treated as a collision rather than overwritten.
            candidate = original.with_name(f"{original.stem}-{number}{original.suffix}")
            number += 1
            resolving_collision = True
            continue
        # An existing, unowned original filename is considered an old download
        # of the current task. This preserves compatibility with the old script.
        return candidate


def perform_downloads(
    db: DB, table: str, tasks: list[tuple[int, str, str, Path]], deadline: float | None
) -> None:
    """Download rows prepared by either the regular or extra-list queue."""
    done = failed = 0
    for image_id, url, local_name, destination in tasks:
        if deadline is not None and time.monotonic() >= deadline:
            log("time budget exhausted; rerun the same command to resume")
            break
        try:
            destination = available_destination(db, table, image_id, url, destination)
            if destination.name != local_name:
                log(f"name collision: {local_name} -> {destination.name}")
                db.conn.execute(
                    f"UPDATE {table} SET local_name=? WHERE id=?", (destination.name, image_id)
                )
                db.commit()
                local_name = destination.name
            if destination.exists() and destination.stat().st_size > 0:
                log(f"exists: {destination}")
            else:
                log(f"GET {destination}")
                download_file(url, destination)
            db.conn.execute(
                f"UPDATE {table} SET status='done',error='',source_path=? WHERE id=?",
                (str(destination.resolve()), image_id),
            )
            db.commit()
            done += 1
        except Exception as error:
            log(f"FAILED {local_name}: {error}")
            db.conn.execute(
                f"UPDATE {table} SET status='failed',error=? WHERE id=?",
                (str(error)[:400], image_id),
            )
            db.commit()
            failed += 1
    log(f"download done: {done} ok/existing, {failed} failed")


def cmd_download(db: DB, args: argparse.Namespace) -> None:
    base = Path(args.base)
    deadline = time.monotonic() + args.budget_minutes * 60 if args.budget_minutes else None
    download_root = (
        input_path(base, args.download_dir)
        if args.download_dir
        else base / DEFAULT_DOWNLOAD_DIR
    )
    download_root.mkdir(parents=True, exist_ok=True)
    use_extra = bool(args.include_files or args.include_directories)

    if use_extra:
        lists = collect_lists(base, args.include_files, args.include_directories)
        keys = register_extra_lists(db, lists, download_root)
        if not keys:
            log("no include lists supplied")
            return
        placeholders = ",".join("?" for _ in keys)
        sql = (
            "SELECT id,list_key,url,local_name FROM extra_images "
            "WHERE status IN ('new','failed') AND url!='' AND local_name!='' "
            f"AND list_key IN ({placeholders}) ORDER BY id"
        )
        params: list[Any] = list(keys)
        if args.limit:
            sql += " LIMIT ?"
            params.append(args.limit)
        rows = db.conn.execute(sql, params).fetchall()
        tasks = [
            (
                image_id,
                url,
                local_name,
                download_root / Path(*list_key.split("/")) / local_name,
            )
            for image_id, list_key, url, local_name in rows
        ]
        log(f"{len(tasks)} extra images pending; root: {download_root}")
        perform_downloads(db, "extra_images", tasks, deadline)
        return

    sql = "SELECT id,url,local_name FROM images WHERE status IN ('new','failed') ORDER BY id"
    if args.limit:
        sql += f" LIMIT {args.limit}"
    rows = db.conn.execute(sql).fetchall()
    tasks = [
        (image_id, url, local_name, download_root / local_name)
        for image_id, url, local_name in rows
    ]
    log(f"{len(tasks)} images pending; root: {download_root}")
    perform_downloads(db, "images", tasks, deadline)


# ---------------------------------------------------------------- favorite ----


def favorite_card_ids(db: DB, entry: str) -> list[str]:
    parsed = parse_url_id(entry)
    if parsed:
        rows = db.conn.execute(
            "SELECT id FROM cards WHERE set_code=? COLLATE NOCASE "
            "AND collector_number=? COLLATE NOCASE",
            parsed,
        ).fetchall()
    else:
        rows = db.conn.execute(
            "SELECT id FROM cards WHERE name=? COLLATE NOCASE "
            "UNION SELECT card_id FROM images WHERE face_name=? COLLATE NOCASE "
            "UNION SELECT card_id FROM extra_images WHERE face_name=? COLLATE NOCASE",
            (entry, entry, entry),
        ).fetchall()
    return [row[0] for row in rows]


def favorite_sources(db: DB, base: Path, card_id: str) -> list[tuple[str, Path]]:
    sources: list[tuple[str, Path]] = []
    rows = db.conn.execute(
        "SELECT face_name,local_name,source_path FROM images "
        "WHERE card_id=? AND status='done' ORDER BY face_index,id",
        (card_id,),
    ).fetchall()
    for _face_name, local_name, source_path in rows:
        candidates = [
            Path(source_path) if source_path else base / DEFAULT_DOWNLOAD_DIR / local_name,
            base / DEFAULT_DOWNLOAD_DIR / local_name,
            base / LEGACY_DOWNLOAD_DIR / local_name,
        ]
        source = next((path for path in candidates if path.is_file()), candidates[0])
        sources.append((local_name, source))
    rows = db.conn.execute(
        "SELECT list_key,local_name,source_path FROM extra_images "
        "WHERE card_id=? AND status='done' ORDER BY face_index,id",
        (card_id,),
    ).fetchall()
    for list_key, local_name, source_path in rows:
        fallback = base / DEFAULT_DOWNLOAD_DIR / Path(*list_key.split("/")) / local_name
        source = Path(source_path) if source_path else fallback
        if not source.is_file():
            source = fallback
        sources.append((local_name, source))
    return sources


def cmd_favorite(db: DB, args: argparse.Namespace) -> None:
    base = Path(args.base)
    files = args.files
    if not files and not args.directories:
        files = [DEFAULT_FAVORITE_FILE]
    lists = collect_lists(base, files, args.directories)
    copied = existing = missing = 0
    for path, list_key in lists:
        target_dir = base / FAVORITE_OUTPUT_DIR / Path(*list_key.split("/"))
        target_dir.mkdir(parents=True, exist_ok=True)
        log(f"favorite list: {path} -> {target_dir}")
        copied_names: set[str] = set()
        for entry in read_entries(path):
            card_ids = favorite_card_ids(db, entry)
            found = False
            for card_id in card_ids:
                for local_name, source in favorite_sources(db, base, card_id):
                    if local_name in copied_names:
                        found = True
                        continue
                    destination = target_dir / local_name
                    if destination.exists():
                        copied_names.add(local_name)
                        existing += 1
                        found = True
                        continue
                    if not source.is_file():
                        continue
                    shutil.copy2(source, destination)
                    copied_names.add(local_name)
                    copied += 1
                    found = True
                    log(f"copy {source} -> {destination}")
            if not found:
                missing += 1
                log(f"NOT DOWNLOADED: {entry}")
    log(f"favorite done: {copied} copied, {existing} existing, {missing} unavailable")


# ------------------------------------------------------------------ status ----


def cmd_status(db: DB, args: argparse.Namespace) -> None:
    for label, sql in [
        ("cards", "SELECT COUNT(*) FROM cards"),
        ("images", "SELECT COUNT(*) FROM images"),
        ("images done", "SELECT COUNT(*) FROM images WHERE status='done'"),
        ("images new", "SELECT COUNT(*) FROM images WHERE status='new'"),
        ("images failed", "SELECT COUNT(*) FROM images WHERE status='failed'"),
        ("extra images", "SELECT COUNT(*) FROM extra_images"),
        ("extra done", "SELECT COUNT(*) FROM extra_images WHERE status='done'"),
        ("extra failed", "SELECT COUNT(*) FROM extra_images WHERE status='failed'"),
    ]:
        print(f"{label:>14}: {db.conn.execute(sql).fetchone()[0]}")
    print(f"{'bulk updated':>14}: {db.get_kv('bulk_updated_at') or 'unknown'}")
    print(f"{'image format':>14}: {db.get_kv('image_format') or IMAGE_FORMAT}")
    print(f"{'base':>14}: {Path(args.base).resolve()}")


# --------------------------------------------------------------------- main ----


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--base",
        default=str(Path(__file__).resolve().parent),
        help="working directory for state/cache/images (default: script directory)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    track = sub.add_parser("track", help="read/refresh bulk data and track matching cards")
    track.add_argument("--update", action="store_true", help="redownload bulk data even if unchanged")
    track.add_argument("--offline", action="store_true", help="use cached bulk data without API requests")
    track.add_argument("--max-cards", type=int, default=None, help="stop after this many matching cards")
    track.add_argument(
        "--image-format",
        choices=tuple(IMAGE_EXTENSIONS),
        default=IMAGE_FORMAT,
        help=f"Scryfall image_uris key (default: {IMAGE_FORMAT})",
    )
    track.add_argument(
        "--all-faces", action="store_true", help="track every card face instead of only the front"
    )

    download = sub.add_parser("download", help="download pending images")
    download.add_argument("--limit", type=int, default=None)
    download.add_argument(
        "--budget-minutes",
        type=float,
        default=None,
        help="stop cleanly after this many minutes (resumable)",
    )
    download.add_argument(
        "--download-dir",
        default=None,
        help=f"root for regular and included downloads (default: BASE/{DEFAULT_DOWNLOAD_DIR})",
    )
    download.add_argument(
        "--include-file",
        dest="include_files",
        nargs="+",
        metavar="FILE",
        help="download URL IDs from text files into DOWNLOAD_DIR/<file>/",
    )
    download.add_argument(
        "--include-dir",
        dest="include_directories",
        nargs="+",
        metavar="DIR",
        help="read .txt lists recursively and preserve their directory structure",
    )

    favorite = sub.add_parser("favorite", help="copy downloaded images into favorite/deck folders")
    favorite.add_argument(
        "--file",
        "--files",
        dest="files",
        nargs="+",
        metavar="FILE",
        help=f"favorite lists (default: BASE/{DEFAULT_FAVORITE_FILE})",
    )
    favorite.add_argument(
        "--dir",
        "--dirs",
        dest="directories",
        nargs="+",
        metavar="DIR",
        help="read favorite .txt lists recursively and preserve their structure",
    )

    sub.add_parser("status", help="print database counts and cache version")
    args = parser.parse_args()
    with DB(Path(args.base)) as db:
        {
            "track": cmd_track,
            "download": cmd_download,
            "favorite": cmd_favorite,
            "status": cmd_status,
        }[args.cmd](db, args)


if __name__ == "__main__":
    main()
