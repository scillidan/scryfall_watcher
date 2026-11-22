# Scryfall Watcher

A local card filter, image downloader, exception-list patcher, and favorite-deck organizer based on Scryfall data. It preserves the old script's bulk filter, filename scheme, and front-face-only default, while adding SQLite state tracking and resumable stages.

- Python 3.10+, standard library only.
- Single-threaded, with 100-150 ms between API requests, `Retry-After` support, and exponential backoff.
- Reuses bulk data, database state, existing image files, and download tasks.
- Writes downloads to `.part` files first, supports HTTP Range resume, and renames files only after completion.

## Download directories

All regular and exception-list downloads share one root directory. By default, this root is `_download/` under `--base`; `--base` itself defaults to the script directory.

| Download type | Default destination |
|---|---|
| Regular cards selected by the bulk filter | `_download/` |
| Extra cards from `download --include-file FILE` | `_download/<file stem>/` |
| Extra cards from `download --include-dir DIR` | `_download/<directory name>/<preserved structure>/` |

For example, with the default base directory, regular images are downloaded to:

```text
scryfall_watcher/_download/
```

Using `--base D:\watcher` changes that default to `D:\watcher\_download\`.

Use `--download-dir` to replace the entire download root for both regular and exception-list downloads:

```bat
python scryfall.py download --download-dir D:\cards
python scryfall.py download --download-dir D:\cards --include-file patches\white.txt
```

These commands write regular images directly to `D:\cards\` and images from `white.txt` to `D:\cards\white\`. A relative `--download-dir` is resolved under `--base`.

Favorites remain separate and are always copied under `<base>/_favorite/`.

### Reusing images downloaded by the old script

Move or copy the old images into `_download/`, or point `--download-dir` at their existing directory. When a tracked filename already exists and is non-empty, the regular downloader marks it as done instead of downloading it again. Names such as `A Golden Opportunity_ywoe_13.png` follow the supported scheme.

The actual source path is recorded for new runs, allowing `favorite` to find images downloaded through a custom `--download-dir`. The favorite source lookup also retains compatibility with the former `scryfall_cards/` default directory.

### Existing files and name collisions

Non-empty existing files are not overwritten. The database associates completed download paths with their source URLs:

- If another task refers to the same source URL, it reuses and skips the existing file.
- If a different source needs the same filename in the same directory, the later file is named `<stem>-2.<ext>`, then `<stem>-3.<ext>`, and so on.
- An unowned file with the exact expected name is accepted as an old download on first encounter, preserving compatibility with files produced by the old script.
- Renamed filenames and source paths are saved in SQLite, so subsequent runs and `favorite` use the allocated names consistently.

Collision numbering applies independently within each destination directory; identical names in different list directories do not affect one another.

## Commands

With [just](https://github.com/casey/just):

```bat
just update          :: Check for new bulk data, then download pending regular images
just update 20       :: Limit this regular download run to 20 minutes
just favorite        :: Read favorite.txt and copy cards to _favorite/favorite/
just status          :: Show progress
just refresh         :: Force a fresh bulk-data download
```

Direct script calls:

```bat
python scryfall.py track
python scryfall.py download --budget-minutes 60
python scryfall.py favorite
python scryfall.py status
```

Common tracking options:

```bat
python scryfall.py track --update             :: Force a bulk-data update
python scryfall.py track --offline            :: Use the cache without API requests
python scryfall.py track --all-faces          :: Track every face of multi-face cards
python scryfall.py track --image-format large :: Use large images
python scryfall.py track --max-cards 20       :: Small trial run
python scryfall.py download --limit 20        :: Process at most 20 pending downloads
python scryfall.py --base D:\watcher track    :: Use another base directory
```

By default, `track` fetches the bulk metadata first. If the version is unchanged, it reuses `cache/bulk_data.jsonl.gz`; otherwise, it downloads the new file. Current Scryfall JSONL.GZ files are decompressed and read one card at a time instead of loading the entire bulk file into memory. The legacy `cache/bulk_data.json` JSON-array format remains supported, including in `--offline` mode. `--update` skips the version comparison. Regular downloads process only `new` and `failed` tasks, so failed tasks are retried automatically on the next run.

## Extra downloads from URL-ID lists

Use text files containing one Scryfall URL ID per line. Blank lines and lines beginning with `#` are ignored. These forms are accepted:

```text
bbd-96-long-road-home
bbd/96/long-road-home
https://scryfall.com/card/bbd/96/long-road-home
```

Download one or more files:

```bat
python scryfall.py download --include-file patches\white.txt patches\green.txt
```

For `patches\white.txt`, images are written to:

```text
_download/white/
```

Read every `.txt` file under one or more directories, including nested directories:

```bat
python scryfall.py download --include-dir patches more_patches
```

For example, `patches\decks\blue.txt` produces:

```text
_download/patches/decks/blue/
```

When `--include-file` or `--include-dir` is present, the command processes only the supplied exception lists; the regular bulk-filter queue is not downloaded in the same run. Resolution results and download failures are stored in SQLite, so rerunning the same command resumes without repeatedly resolving valid entries.

## Favorites and deck-like folders

The favorite command and its arguments are independent of the download-directory settings. A favorite list accepts either a URL ID or an exact downloaded card/face name, one per line:

```text
A Golden Opportunity
bbd-96-long-road-home
https://scryfall.com/card/bbd/96/long-road-home
```

The default command reads `favorite.txt` under `--base`:

```bat
just favorite
```

It copies the corresponding downloaded images to:

```text
_favorite/favorite/
```

Use one or more custom files:

```bat
python scryfall.py favorite --file decks\control.txt decks\ramp.txt
just favorite --file decks\control.txt decks\ramp.txt
```

These lists produce `_favorite/control/` and `_favorite/ramp/`. Naming a list after a deck therefore gives it a matching deck-like output directory.

Directories are also supported:

```bat
python scryfall.py favorite --dir deck_lists
```

A file named `deck_lists\white\control.txt` is copied to `_favorite/deck_lists/white/control/`. The `--dirs` and `--files` aliases are also accepted.

`favorite` copies only images already downloaded by regular or exception-list tasks; it does not download missing cards. Unavailable entries are logged as `NOT DOWNLOADED`. Existing destination files are left untouched, and source images are never moved or deleted. If multiple faces were downloaded for a card, all available downloaded faces are copied.

## Regular bulk-filter rules

The query description retained from the old script is:

```text
date>=soi (c:c or c>=w or c>=g) (r:uncommon or r:rare or r:mythic)
```

The local filter preserves the old script's actual behavior:

- `released_at >= 2016-04-08`;
- rarity is `uncommon`, `rare`, or `mythic`;
- `colors` is empty or contains `C`, or `color_identity` contains `W` or `G`.

This is a fixed local filter, not a general-purpose Scryfall query parser. Because the regular tracker uses `oracle_cards`, it takes only the representative printing selected by the bulk file for each Oracle ID rather than downloading every reprint. Exception lists instead resolve the exact set code and collector number in each URL ID.

## Image names

The default format is `png`, and only the front face is tracked. The front face of a double-faced card still has the `_face1` suffix. With `track --all-faces`, `_face2` and any additional faces are also tracked. Single-face cards use this filename scheme:

```text
<card name>_<set code>_<collector number>.png
```

Windows-invalid characters are replaced, and each filename component is length-capped. Tasks created with different regular-tracker image formats remain independent; earlier tasks and old images are not deleted automatically.

## Project layout

```text
cache/bulk_data.jsonl.gz  Current Scryfall JSONL.GZ bulk data
cache/bulk_data.json      Optional legacy JSON-array cache
state.sqlite3             Cards, regular/extra tasks, source paths, and bulk version
_download/             Default shared root for regular and exception-list images
_favorite/             Copied favorites organized by list/deck name
favorite.txt           Optional default favorite list
scryfall.py            Single-file program
justfile               Common commands
```
