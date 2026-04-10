from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import ssl
import sys
from dataclasses import dataclass
from typing import Any
from urllib import parse, request


@dataclass(frozen=True)
class SongState:
    id: str
    key: str
    starred: bool
    rating: int


@dataclass
class ActionPlan:
    stars: list[str]
    unstars: list[str]
    ratings: list[tuple[str, int]]


@dataclass(frozen=True)
class Config:
    primary_url: str
    primary_username: str
    primary_password: str
    secondary_url: str
    secondary_username: str
    secondary_password: str
    client_name: str
    request_timeout_sec: int
    primary_verify_tls: bool
    secondary_verify_tls: bool
    sync_favorites: bool
    sync_ratings: bool
    strict_favorites: bool
    strict_ratings: bool
    dry_run: bool


@dataclass(frozen=True)
class LibraryIndex:
    songs: dict[str, SongState]
    total_songs_seen: int
    skipped_missing_id: int
    skipped_ambiguous: int


class SubsonicClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        client_name: str,
        timeout_sec: int,
        verify_tls: bool,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.client_name = client_name
        self.timeout_sec = timeout_sec
        self.verify_tls = verify_tls

    def list_album_ids(self, size: int = 500, album_limit: int | None = None) -> list[str]:
        album_ids: list[str] = []
        offset = 0
        while True:
            response = self._call(
                "getAlbumList2",
                {
                    "type": "alphabeticalByArtist",
                    "size": size,
                    "offset": offset,
                },
            )
            albums = _as_list(response.get("albumList2", {}).get("album"))
            if not albums:
                break

            for album in albums:
                album_id = str(album.get("id", "")).strip()
                if album_id:
                    album_ids.append(album_id)
                    if album_limit is not None and len(album_ids) >= album_limit:
                        return album_ids

            if len(albums) < size:
                break
            offset += size

        return album_ids

    def get_album_songs(self, album_id: str) -> list[dict[str, Any]]:
        response = self._call("getAlbum", {"id": album_id})
        return _as_list(response.get("album", {}).get("song"))

    def star(self, song_id: str) -> None:
        self._call("star", {"id": song_id})

    def unstar(self, song_id: str) -> None:
        self._call("unstar", {"id": song_id})

    def set_rating(self, song_id: str, rating: int) -> None:
        self._call("setRating", {"id": song_id, "rating": rating})

    def _call(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        salt = secrets.token_hex(8)
        token = hashlib.md5(f"{self.password}{salt}".encode("utf-8")).hexdigest()

        query_params: dict[str, Any] = {
            "u": self.username,
            "t": token,
            "s": salt,
            "v": "1.16.1",
            "c": self.client_name,
            "f": "json",
        }
        if params:
            query_params.update(params)

        url = f"{self._endpoint_url(endpoint)}?{parse.urlencode(query_params, doseq=True)}"

        ssl_context = None
        if not self.verify_tls:
            ssl_context = ssl._create_unverified_context()

        req = request.Request(url, method="GET")
        with request.urlopen(req, timeout=self.timeout_sec, context=ssl_context) as resp:
            payload = json.load(resp)

        subsonic = payload.get("subsonic-response")
        if not isinstance(subsonic, dict):
            raise RuntimeError(f"Unexpected API response from {endpoint}: missing subsonic-response")

        if subsonic.get("status") != "ok":
            err = subsonic.get("error", {})
            code = err.get("code", "?")
            message = err.get("message", "Unknown error")
            raise RuntimeError(f"API error for {endpoint}: code={code} message={message}")

        return subsonic

    def _endpoint_url(self, endpoint: str) -> str:
        clean_endpoint = endpoint.removesuffix(".view")
        if self.base_url.endswith("/rest"):
            return f"{self.base_url}/{clean_endpoint}.view"
        if "/rest/" in self.base_url:
            return f"{self.base_url.rstrip('/')}/{clean_endpoint}.view"
        return f"{self.base_url}/rest/{clean_endpoint}.view"


def canonical_song_key(song: dict[str, Any]) -> str:
    raw_path = str(song.get("path", "")).strip()
    if raw_path:
        normalized_path = raw_path.replace("\\", "/").strip("/").lower()
        return f"path:{normalized_path}"

    artist = _norm(song.get("albumArtist") or song.get("artist"))
    album = _norm(song.get("album"))
    disc = str(_safe_int(song.get("discNumber") or song.get("disc"), 0))
    track = str(_safe_int(song.get("track") or song.get("trackNumber"), 0))
    title = _norm(song.get("title"))
    duration = str(_safe_int(song.get("duration"), 0))

    return f"meta:{artist}|{album}|{disc}|{track}|{title}|{duration}"


def compute_actions(
    source: dict[str, SongState],
    target: dict[str, SongState],
    strict_favorites: bool,
    strict_ratings: bool,
    sync_favorites: bool,
    sync_ratings: bool,
) -> ActionPlan:
    stars: list[str] = []
    unstars: list[str] = []
    ratings: list[tuple[str, int]] = []

    shared_keys = source.keys() & target.keys()
    for key in shared_keys:
        src = source[key]
        dst = target[key]

        if sync_favorites:
            if src.starred and not dst.starred:
                stars.append(dst.id)
            elif strict_favorites and not src.starred and dst.starred:
                unstars.append(dst.id)

        if sync_ratings and src.rating != dst.rating:
            if src.rating > 0 or strict_ratings:
                ratings.append((dst.id, src.rating))

    stars.sort()
    unstars.sort()
    ratings.sort(key=lambda x: x[0])
    return ActionPlan(stars=stars, unstars=unstars, ratings=ratings)


def fetch_library_index(
    client: SubsonicClient,
    name: str,
    album_limit: int | None,
) -> LibraryIndex:
    album_ids = client.list_album_ids(album_limit=album_limit)
    if not album_ids:
        print(f"[{name}] no albums returned")

    songs: dict[str, SongState] = {}
    ambiguous_keys: set[str] = set()
    total_songs_seen = 0
    skipped_missing_id = 0

    for idx, album_id in enumerate(album_ids, start=1):
        album_songs = client.get_album_songs(album_id)
        for raw_song in album_songs:
            total_songs_seen += 1
            song_id = str(raw_song.get("id", "")).strip()
            if not song_id:
                skipped_missing_id += 1
                continue

            key = canonical_song_key(raw_song)
            if key in ambiguous_keys:
                continue

            if key in songs:
                songs.pop(key, None)
                ambiguous_keys.add(key)
                continue

            songs[key] = SongState(
                id=song_id,
                key=key,
                starred=bool(raw_song.get("starred")),
                rating=_parse_rating(raw_song),
            )

        if idx % 100 == 0:
            print(f"[{name}] processed albums: {idx}/{len(album_ids)}")

    return LibraryIndex(
        songs=songs,
        total_songs_seen=total_songs_seen,
        skipped_missing_id=skipped_missing_id,
        skipped_ambiguous=len(ambiguous_keys),
    )


def apply_actions(client: SubsonicClient, plan: ActionPlan, dry_run: bool) -> None:
    if dry_run:
        print("Dry run enabled, no writes will be sent.")
        return

    for song_id in plan.stars:
        client.star(song_id)

    for song_id in plan.unstars:
        client.unstar(song_id)

    for song_id, rating in plan.ratings:
        client.set_rating(song_id, rating)


def load_config(env_path: str) -> Config:
    file_values = _load_env_file(env_path)

    def read(name: str, default: str | None = None) -> str:
        if name in os.environ:
            return os.environ[name]
        if name in file_values:
            return file_values[name]
        if default is not None:
            return default
        raise ValueError(f"Missing required config: {name}")

    return Config(
        primary_url=read("PRIMARY_URL"),
        primary_username=read("PRIMARY_USERNAME"),
        primary_password=read("PRIMARY_PASSWORD"),
        secondary_url=read("SECONDARY_URL"),
        secondary_username=read("SECONDARY_USERNAME"),
        secondary_password=read("SECONDARY_PASSWORD"),
        client_name=read("CLIENT_NAME", "navidrome-sync"),
        request_timeout_sec=_safe_int(read("REQUEST_TIMEOUT_SEC", "30"), 30),
        primary_verify_tls=_parse_bool(read("PRIMARY_VERIFY_TLS", "true")),
        secondary_verify_tls=_parse_bool(read("SECONDARY_VERIFY_TLS", "true")),
        sync_favorites=_parse_bool(read("SYNC_FAVORITES", "true")),
        sync_ratings=_parse_bool(read("SYNC_RATINGS", "true")),
        strict_favorites=_parse_bool(read("STRICT_FAVORITES", "false")),
        strict_ratings=_parse_bool(read("STRICT_RATINGS", "false")),
        dry_run=_parse_bool(read("DRY_RUN", "true")),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-way Navidrome sync (primary -> secondary) for favorites and ratings"
    )
    parser.add_argument("--env-file", default=".env", help="Path to .env file")
    parser.add_argument("--album-limit", type=int, default=None, help="Limit albums for testing")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Force dry-run mode")
    mode.add_argument("--apply", action="store_true", help="Apply writes to secondary")

    args = parser.parse_args(argv)

    try:
        config = load_config(args.env_file)

        dry_run = config.dry_run
        if args.dry_run:
            dry_run = True
        if args.apply:
            dry_run = False

        source_client = SubsonicClient(
            base_url=config.primary_url,
            username=config.primary_username,
            password=config.primary_password,
            client_name=config.client_name,
            timeout_sec=config.request_timeout_sec,
            verify_tls=config.primary_verify_tls,
        )
        target_client = SubsonicClient(
            base_url=config.secondary_url,
            username=config.secondary_username,
            password=config.secondary_password,
            client_name=config.client_name,
            timeout_sec=config.request_timeout_sec,
            verify_tls=config.secondary_verify_tls,
        )

        print("Building source library index...")
        source_index = fetch_library_index(source_client, "source", args.album_limit)

        print("Building target library index...")
        target_index = fetch_library_index(target_client, "target", args.album_limit)

        plan = compute_actions(
            source=source_index.songs,
            target=target_index.songs,
            strict_favorites=config.strict_favorites,
            strict_ratings=config.strict_ratings,
            sync_favorites=config.sync_favorites,
            sync_ratings=config.sync_ratings,
        )

        shared = len(source_index.songs.keys() & target_index.songs.keys())
        print("--- Sync Summary ---")
        print(f"Source indexed songs: {len(source_index.songs)} (seen={source_index.total_songs_seen}, missing_id={source_index.skipped_missing_id}, ambiguous_keys={source_index.skipped_ambiguous})")
        print(f"Target indexed songs: {len(target_index.songs)} (seen={target_index.total_songs_seen}, missing_id={target_index.skipped_missing_id}, ambiguous_keys={target_index.skipped_ambiguous})")
        print(f"Matched songs by key: {shared}")
        print(f"Will star on target: {len(plan.stars)}")
        print(f"Will unstar on target: {len(plan.unstars)}")
        print(f"Will set ratings on target: {len(plan.ratings)}")

        apply_actions(target_client, plan, dry_run=dry_run)
        if dry_run:
            print("Dry run complete.")
        else:
            print("Sync complete.")

        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _load_env_file(path: str) -> dict[str, str]:
    if not os.path.exists(path):
        return {}

    values: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()

            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"\"", "'"}:
                value = value[1:-1]

            values[key] = value

    return values


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_rating(song: dict[str, Any]) -> int:
    rating = _safe_int(song.get("userRating"), 0)
    if rating < 0:
        return 0
    if rating > 5:
        return 5
    return rating


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]
