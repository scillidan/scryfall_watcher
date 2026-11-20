# Scryfall Watcher

A local card filter and image downloader based on Scryfall's daily `oracle_cards` bulk data. It preserves the old script's filtering logic, filename scheme, and front-face-only default, while adding SQLite state tracking and resumable stages.

- Python 3.10+, standard library only.
- Single-threaded, with 100-150 ms between API requests, `Retry-After` support, and exponential backoff.
- Reuses bulk data, database state, and download tasks. After an interruption, rerun the same command to continue.
- Writes images to `.part` files first, supports HTTP Range resume, and renames files only after completion.

## Usage

Install Python. [uv](https://docs.astral.sh/uv/) and [just](https://github.com/casey/just) are optional convenience tools.

```bat
just update          :: Check for new bulk data, then download pending images
just update 20       :: Limit this download run to 20 minutes
just status          :: Show progress
just refresh         :: Force a fresh bulk-data download
```

You can also call the script directly:

```bat
python scryfall.py track
python scryfall.py download --budget-minutes 60
python scryfall.py status
```

Common options:

```bat
python scryfall.py track --update             :: Force a bulk-data update
python scryfall.py track --offline            :: Use the cache without API requests
python scryfall.py track --all-faces          :: Track every face of multi-face cards
python scryfall.py track --image-format large :: Use large images
python scryfall.py track --max-cards 20       :: Small trial run
python scryfall.py download --limit 20        :: Process at most 20 pending downloads
python scryfall.py download --output D:\cards :: Custom image directory
python scryfall.py --base D:\watcher track    :: Use another base directory
```

By default, `track` fetches the bulk metadata first. If the version is unchanged, it reuses `cache/bulk_data.json`; otherwise, it downloads the new file. `--update` skips the version comparison. The download stage processes only `new` and `failed` tasks, so failed tasks are retried automatically on the next run.

## Filter rules

The query description retained from the old script is:

```text
date>=soi (c:c or c>=w or c>=g) (r:uncommon or r:rare or r:mythic)
```

The local filter preserves the old script's actual behavior:

- `released_at >= 2016-04-08`;
- rarity is `uncommon`, `rare`, or `mythic`;
- `colors` is empty or contains `C`, or `color_identity` contains `W` or `G`.

This is a fixed local filter, not a general-purpose Scryfall query parser. Because the program uses `oracle_cards`, it takes only the representative printing selected by the bulk file for each Oracle ID rather than downloading every reprint.

## Image rules

The default format is `png`, and only the front face is tracked. The front face of a double-faced card still has the `_face1` suffix. With `--all-faces`, `_face2` and any additional faces are also tracked. Single-face cards use this filename scheme:

```text
<card name>_<set code>_<collector number>.png
```

Windows-invalid characters are removed, and each filename component is length-capped. Tasks created with different image formats or face settings remain independent; earlier tasks and old images are not deleted automatically.

## Layout

```text
cache/bulk_data.json   Original Scryfall bulk data
state.sqlite3          Cards, image tasks, and bulk-version state
scryfall_cards/        Default image output directory
scryfall.py            Single-file program
justfile               Common commands
```

The cache, database, and default image directory all live under `--base`. Images are written elsewhere only when `--output` is explicitly provided.
