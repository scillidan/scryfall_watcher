# Scryfall watcher — resumable bulk-data tracker and image downloader
#   just update       # track latest bulk data -> download (60-minute budget)
#   just update 20    # custom download budget in minutes
#   just refresh      # force a fresh bulk-data download
#   just favorite     # copy cards from favorite.txt into _favorite/favorite/
#   just status       # progress

set windows-shell := ["cmd", "/c"]

update db="60":
    just track
    just download {{db}}

track:
    uv run python scryfall.py track

refresh:
    uv run python scryfall.py track --update

download db="60":
    uv run python scryfall.py download --budget-minutes {{db}}

favorite *args:
    uv run python scryfall.py favorite {{args}}

status:
    uv run python scryfall.py status
