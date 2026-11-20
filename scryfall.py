#!/usr/bin/env python3
"""Polite, resumable Scryfall bulk-data tracker and card-image downloader.

Subcommands:
  track     Refresh/read Oracle Cards bulk data and record matching images.
  download  Download pending images with atomic, resumable writes.
  status    Print database and cache progress.

Python 3.10+, standard library only. State lives in state.sqlite3; interrupting
and rerunning a command is safe.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import http.client
import json
import random
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# This query documents the filter below; this program does not evaluate arbitrary
# Scryfall syntax locally. The date is Shadows over Innistrad's release date.
SEARCH_QUERY = "date>=soi (c:c or c>=w or c>=g) (r:uncommon or r:rare or r:mythic)"
START_DATE = "2016-04-08"
BULK_DATA_TYPE = "oracle_cards"
BULK_INFO_URL = f"https://api.scryfall.com/bulk-data/{BULK_DATA_TYPE}"
DEFAULT_OUTPUT_DIR = "scryfall_cards"
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
  status TEXT NOT NULL DEFAULT 'new',  -- new | done | failed
  error TEXT NOT NULL DEFAULT '',
  UNIQUE(card_id, face_index, image_format)
);
CREATE INDEX IF NOT EXISTS idx_images_status ON images(status, id);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


class DB:
    def __init__(self, base: Path):
        base.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(base / "state.sqlite3")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

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


def load_bulk(path: Path) -> list[dict[str, Any]]:
    log(f"loading bulk data: {path}")
    try:
        with path.open("r", encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read bulk data {path}: {error}") from error
    if not isinstance(data, list):
        raise RuntimeError(f"bulk data is not a JSON array: {path}")
    return data


def prepare_bulk(db: DB, base: Path, update: bool, offline: bool) -> tuple[list[dict[str, Any]], Path]:
    cache = base / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    bulk_path = cache / "bulk_data.json"

    if offline:
        if not bulk_path.exists():
            raise RuntimeError("offline mode requires cache/bulk_data.json; run track online first")
        return load_bulk(bulk_path), bulk_path

    info = fetch_json(BULK_INFO_URL)
    download_uri = info.get("download_uri", "")
    updated_at = info.get("updated_at", "")
    if not download_uri:
        raise RuntimeError("bulk-data response has no download_uri")
    version = f"{updated_at}|{download_uri}"
    current = bulk_path.exists() and not update and db.get_kv("bulk_version") == version

    if current:
        log(f"bulk data unchanged ({updated_at or 'unknown update time'}); using cache")
    else:
        size = info.get("compressed_size")
        size_text = f", {int(size) / 1e6:.1f} MB" if isinstance(size, int) else ""
        log(f"downloading {BULK_DATA_TYPE} bulk data ({updated_at or 'unknown'}{size_text})")
        download_file(download_uri, bulk_path)

    cards = load_bulk(bulk_path)  # validate before recording this version
    db.set_kv("bulk_version", version)
    db.set_kv("bulk_updated_at", str(updated_at))
    db.set_kv("bulk_download_uri", str(download_uri))
    db.commit()
    return cards, bulk_path


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


def cmd_track(db: DB, args: argparse.Namespace) -> None:
    if args.offline and args.update:
        raise SystemExit("--offline and --update cannot be used together")
    base = Path(args.base)
    all_cards, bulk_path = prepare_bulk(db, base, args.update, args.offline)
    log(f"{len(all_cards)} cards in {bulk_path.name}")
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

    db.set_kv("image_format", args.image_format)
    db.set_kv("only_front_face", str(not args.all_faces))
    db.commit()
    log(
        f"track done: {matched} matched, {new_cards} new cards, "
        f"{new_images} new images, {without_images} without requested image"
    )


# ---------------------------------------------------------------- download ----


def cmd_download(db: DB, args: argparse.Namespace) -> None:
    base = Path(args.base)
    output = Path(args.output) if args.output else base / DEFAULT_OUTPUT_DIR
    output.mkdir(parents=True, exist_ok=True)
    rows = db.conn.execute(
        "SELECT id,url,local_name FROM images WHERE status IN ('new','failed') "
        "ORDER BY id" + (f" LIMIT {args.limit}" if args.limit else "")
    ).fetchall()
    log(f"{len(rows)} images pending; output: {output}")
    deadline = time.monotonic() + args.budget_minutes * 60 if args.budget_minutes else None
    done = failed = 0

    for image_id, url, local_name in rows:
        if deadline is not None and time.monotonic() >= deadline:
            log("time budget exhausted; rerun the same command to resume")
            break
        if not local_name:
            message = "empty local_name; run track again"
            db.conn.execute(
                "UPDATE images SET status='failed',error=? WHERE id=?", (message, image_id)
            )
            db.commit()
            failed += 1
            continue
        destination = output / local_name
        if destination.exists() and destination.stat().st_size > 0:
            db.conn.execute(
                "UPDATE images SET status='done',error='' WHERE id=?", (image_id,)
            )
            db.commit()
            done += 1
            log(f"exists: {local_name}")
            continue
        try:
            log(f"GET {local_name}")
            download_file(url, destination)
            db.conn.execute(
                "UPDATE images SET status='done',error='' WHERE id=?", (image_id,)
            )
            db.commit()
            done += 1
        except Exception as error:
            log(f"FAILED {local_name}: {error}")
            db.conn.execute(
                "UPDATE images SET status='failed',error=? WHERE id=?",
                (str(error)[:400], image_id),
            )
            db.commit()
            failed += 1
    log(f"download done: {done} ok/existing, {failed} failed")


# ------------------------------------------------------------------ status ----


def cmd_status(db: DB, args: argparse.Namespace) -> None:
    for label, sql in [
        ("cards", "SELECT COUNT(*) FROM cards"),
        ("images", "SELECT COUNT(*) FROM images"),
        ("images done", "SELECT COUNT(*) FROM images WHERE status='done'"),
        ("images new", "SELECT COUNT(*) FROM images WHERE status='new'"),
        ("images failed", "SELECT COUNT(*) FROM images WHERE status='failed'"),
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
        "--output",
        default=None,
        help=f"image directory (default: BASE/{DEFAULT_OUTPUT_DIR})",
    )

    sub.add_parser("status", help="print database counts and cache version")
    args = parser.parse_args()
    with DB(Path(args.base)) as db:
        {"track": cmd_track, "download": cmd_download, "status": cmd_status}[args.cmd](db, args)


if __name__ == "__main__":
    main()
