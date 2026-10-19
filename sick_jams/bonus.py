"""Bonus mix built around one band, album or song you're into right now.

The input is matched against your library first (artist, then album, then song) and then against
Deezer, so it works for things you don't own yet. The mix itself only uses songs from your library:
the seed artist, artists Deezer says are related, and a little from the same vibe.
"""

import difflib
import random
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .mixer import Profile, Settings, assemble, freshness, hit_scores
from .music import Track
from .popularity import Popularity, norm

SEED_CAP = 10          # max songs by the seed artist
MIN_POOL = 15


@dataclass
class Seed:
    kind: str                          # "artist", "album" or "song"
    label: str                         # what the playlist is named after
    artist: str                        # as named in your library if you have them, else Deezer's
    artist_id: int | None
    tracks: list[Track] = field(default_factory=list)   # matching library tracks (album/song)


def resolve(query: str, tracks: list[Track], pop: Popularity) -> Seed | None:
    q = norm(query)
    if not q:
        return None
    artists = {norm(t.artist): t.artist for t in tracks if t.artist}

    def artist_id(name):
        return pop.artist(name).get("id")

    def best_of(ts):
        return max(ts, key=lambda t: (t.plays, t.favorited))

    # "Artist - Song", "Song by Artist", "Artist Song"
    halves = re.split(r"\s+-\s+|\s+by\s+", query, maxsplit=1, flags=re.I)
    pairs = []
    if len(halves) == 2:
        pairs = [(norm(halves[0]), norm(halves[1])), (norm(halves[1]), norm(halves[0]))]

    if q in artists:
        a = artists[q]
        return Seed("artist", a, a, artist_id(a))

    albums, songs = defaultdict(list), defaultdict(list)
    for t in tracks:
        albums[norm(t.album)].append(t)
        songs[norm(t.name)].append(t)
    for kind, index in (("album", albums), ("song", songs)):
        cands = index.get(q, [])
        for a_part, t_part in pairs:
            cands = cands or [t for t in index.get(t_part, []) if norm(t.artist) == a_part]
        if not cands:
            # "rancid time bomb": artist name followed by title
            for na, a in artists.items():
                if len(na) > 2 and q.startswith(na) and q[len(na):] in index:
                    cands = [t for t in index[q[len(na):]] if t.artist == a]
                    if cands:
                        break
        if cands:
            top = best_of(cands)
            if kind == "album":
                album = [t for t in cands if t.artist == top.artist]
                return Seed("album", top.album, top.artist, artist_id(top.artist), album)
            return Seed("song", top.name, top.artist, artist_id(top.artist), [top])

    close = difflib.get_close_matches(q, list(artists), n=1, cutoff=0.85)
    if close:
        a = artists[close[0]]
        return Seed("artist", a, a, artist_id(a))

    hit = pop.search(query)           # not in your library: ask Deezer who it is
    if not hit:
        return None
    lib_name = artists.get(norm(hit["artist"]), hit["artist"])
    kind = {"artist": "artist", "album": "album", "track": "song"}[hit["kind"]]
    return Seed(kind, hit["title"] or lib_name, lib_name, hit["artist_id"])


def build_bonus(seed: Seed, prof: Profile, pop: Popularity, s: Settings,
                recently_mixed: set[str], rng_seed: int | None = None):
    """Returns (tracks, deep cut ids, description) or None if there's too little to work with."""
    rng = random.Random(rng_seed)
    lib = {norm(a): a for a in prof.artist_aff}
    seed_lib = lib.get(norm(seed.artist))

    # How central each library artist is: the seed, then Deezer's related artists, then their
    # related artists, then the rest of the seed's vibe.
    a_norm: dict[str, float] = {}
    if seed_lib:
        a_norm[seed_lib] = 1.0
    related = pop.related_by_id(seed.artist_id) if seed.artist_id else []
    for i, r in enumerate(related):
        a = lib.get(norm(r["name"]))
        if a and a not in a_norm:
            a_norm[a] = 0.85 / (1 + 0.08 * i)
    if len(a_norm) < 8:
        for r in related[:6]:
            for i, r2 in enumerate(pop.related_by_id(r["id"])):
                a = lib.get(norm(r2["name"]))
                if a and a not in a_norm:
                    a_norm[a] = 0.4 / (1 + 0.08 * i)
    pop.save()

    # Fill from the vibe the seed's neighbourhood mostly lives in, which beats trusting one
    # artist's own (possibly odd) tags.
    vibes = Counter()
    for a, w in a_norm.items():
        if a in prof.vibes:
            vibes[prof.vibes[a]] += w
    vibe = vibes.most_common(1)[0][0] if vibes else None
    if vibe:
        top = max((prof.artist_aff[a] for a, v in prof.vibes.items() if v == vibe), default=0) or 1
        for a, v in prof.vibes.items():
            if v == vibe and a not in a_norm:
                a_norm[a] = 0.05 + 0.1 * prof.artist_aff[a] / top

    pool = [t for t in prof.tracks if t.artist in a_norm]
    if len(pool) < MIN_POOL:
        return None

    hit = hit_scores(prof.tracks, pop)
    if seed.kind == "album":
        hit = dict(hit)
        for t in seed.tracks:
            hit[t.pid] = min(1.0, hit.get(t.pid, 0) + 0.4)
    # Open with the song you named, or the biggest hit from the album/artist you named.
    if seed.kind == "song":
        pinned = [t for t in seed.tracks if t in pool][:1]
    else:
        opener = seed.tracks or [t for t in pool if t.artist == seed_lib]
        pinned = [max(opener, key=lambda t: hit[t.pid])] if opener else []
    fresh = freshness(datetime.now(timezone.utc), recently_mixed)
    final, deep = assemble(pool, a_norm, hit, s, rng, fresh,
                           caps={seed_lib: SEED_CAP} if seed_lib else None, pinned=pinned,
                           base=0.03)

    others = [a for a, _ in Counter({t.artist: a_norm[t.artist] for t in final
                                     if t.artist != seed_lib}).most_common(3)]
    what = {"artist": seed.label, "album": f"{seed.label} by {seed.artist}",
            "song": f"\"{seed.label}\" by {seed.artist}"}[seed.kind]
    desc = f"Built around {what}, with {', '.join(others)} and more."
    return final, {t.pid for t in deep}, desc
