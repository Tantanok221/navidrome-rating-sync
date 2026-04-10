# navidrome-rating-sync

One-way sync tool for Navidrome metadata:
- Favorites (heart/star)
- Ratings (0-5)

Direction is always: **primary -> secondary**.

## What It Does

- Builds a song index on both servers using the Subsonic/OpenSubsonic API.
- Matches songs by `path` when available, otherwise by normalized metadata key.
- Computes delta and applies to secondary:
  - `star` / `unstar`
  - `setRating`
- Supports dry-run for safe preview.

## Prerequisites

- Python 3.12+
- `uv`
- Both Navidrome users can read library metadata.
- Secondary user can modify favorites/ratings.

## Setup

```bash
cp .env.example .env
uv sync
```

Fill `.env` with real credentials.

## Run

Dry-run (recommended first):

```bash
uv run navidrome-sync --env-file .env --dry-run
```

Apply changes to secondary:

```bash
uv run navidrome-sync --env-file .env --apply
```

Limit scope for fast testing:

```bash
uv run navidrome-sync --env-file .env --dry-run --album-limit 100
```

Run tests:

```bash
uv run pytest -q
```

## Config Notes

- `STRICT_FAVORITES=false`: only add missing hearts on secondary; do not remove extra hearts.
- `STRICT_RATINGS=false`: only push positive ratings from primary; do not clear secondary ratings to `0`.
- Set either strict flag to `true` for full one-way reconciliation.
- Keep `DRY_RUN=true` in `.env` until first validation is complete.

## Matching Caveats

If song IDs differ between servers, matching relies on:
1. `path` (best)
2. fallback metadata key (`artist|album|disc|track|title|duration`)

If both servers expose different paths/metadata for the same tracks, some songs may not match.

## Security

- `.env` is ignored by git.
- Never commit credentials.
- Use dedicated app users with least privilege.
