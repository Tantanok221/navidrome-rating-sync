from navidrome_sync.sync import SongState, canonical_song_key, compute_actions


def test_canonical_key_prefers_path():
    song = {
        "id": "s1",
        "path": "Artist/Album/01 - Song.flac",
        "title": "Song",
        "artist": "Artist",
    }
    assert canonical_song_key(song) == "path:artist/album/01 - song.flac"


def test_canonical_key_falls_back_to_metadata():
    song = {
        "id": "s1",
        "title": "Song",
        "artist": "Artist",
        "album": "Album",
        "track": 1,
        "discNumber": 1,
        "duration": 217,
    }
    assert canonical_song_key(song) == "meta:artist|album|1|1|song|217"


def test_compute_actions_non_strict_only_updates_positive_source_values():
    source = {
        "k1": SongState(id="src-1", key="k1", starred=True, rating=5),
        "k2": SongState(id="src-2", key="k2", starred=False, rating=0),
    }
    target = {
        "k1": SongState(id="tgt-1", key="k1", starred=False, rating=2),
        "k2": SongState(id="tgt-2", key="k2", starred=True, rating=4),
    }

    plan = compute_actions(
        source,
        target,
        strict_favorites=False,
        strict_ratings=False,
        sync_favorites=True,
        sync_ratings=True,
    )

    assert plan.stars == ["tgt-1"]
    assert plan.unstars == []
    assert plan.ratings == [("tgt-1", 5)]


def test_compute_actions_strict_reconciles_to_source_truth():
    source = {
        "k1": SongState(id="src-1", key="k1", starred=False, rating=0),
        "k2": SongState(id="src-2", key="k2", starred=True, rating=3),
    }
    target = {
        "k1": SongState(id="tgt-1", key="k1", starred=True, rating=4),
        "k2": SongState(id="tgt-2", key="k2", starred=False, rating=0),
    }

    plan = compute_actions(
        source,
        target,
        strict_favorites=True,
        strict_ratings=True,
        sync_favorites=True,
        sync_ratings=True,
    )

    assert plan.stars == ["tgt-2"]
    assert plan.unstars == ["tgt-1"]
    assert sorted(plan.ratings) == [("tgt-1", 0), ("tgt-2", 3)]


def test_compute_actions_skips_unmatched_items():
    source = {
        "only-on-source": SongState(id="src-1", key="only-on-source", starred=True, rating=5),
    }
    target = {
        "only-on-target": SongState(id="tgt-1", key="only-on-target", starred=False, rating=0),
    }

    plan = compute_actions(
        source,
        target,
        strict_favorites=True,
        strict_ratings=True,
        sync_favorites=True,
        sync_ratings=True,
    )

    assert plan.stars == []
    assert plan.unstars == []
    assert plan.ratings == []
