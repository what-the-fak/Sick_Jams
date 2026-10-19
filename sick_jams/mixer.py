"""Builds Daily-Mix-style playlists: one per vibe you've been into lately.

Each mix leans on artists you've played recently, is mostly made of hits (by Deezer popularity and
by how big the song is relative to the artist's other songs), and slips in a few deep cuts.
"""

import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .genres import artist_vibes
from .music import Track
from .popularity import Popularity


@dataclass
class Settings:
    mixes: int = 6
    tracks_per_mix: int = 50
    deep_cut_ratio: float = 0.2
    max_per_artist: int = 4
    min_pool: int = 60
    recent_half_life_days: float = 14.0
    always_include: list[str] = field(default_factory=list)
    never_include: list[str] = field(default_factory=list)


@dataclass
class Mix:
    vibe: str
    tracks: list[Track]
    seed_artists: list[str]
    deep_cuts: set[str]


def _days_ago(dt: datetime | None, now: datetime) -> float | None:
    return (now - dt).total_seconds() / 86400 if dt else None


def track_affinity(t: Track, now: datetime, history: list[dict], half_life: float) -> float:
    """How much you've been into this track lately."""
    a = 0.0
    d = _days_ago(t.last_played, now)
    if d is not None:
        a += 0.5 ** (d / half_life)
    for ev in history:                          # plays we've seen since earlier runs
        n = ev["plays"].get(t.pid)
        if n:
            a += 0.5 * n * 0.5 ** ((now.timestamp() - ev["t"]) / 86400 / half_life)
    a += 0.05 * math.log1p(t.plays)
    if t.favorited:
        a += 0.5
    return a


def _pct_ranks(values: dict[str, float]) -> dict[str, float]:
    order = sorted(values, key=values.get)
    n = max(1, len(order) - 1)
    return {k: i / n for i, k in enumerate(order)}


def hit_scores(tracks: list[Track], pop: Popularity) -> dict[str, float]:
    """0..1, higher = more of a hit."""
    fans = {a: pop.artist(a).get("fans", 0) for a in {t.artist for t in tracks}}
    fame = _pct_ranks({a: math.log1p(f) for a, f in fans.items()})
    ranks = {t.pid: pop.track_rank(t.artist, t.name) for t in tracks}
    ranked = {pid: r for pid, r in ranks.items() if r}
    global_pct = _pct_ranks(ranked)
    artist_max = defaultdict(int)
    for t in tracks:
        if ranks[t.pid]:
            artist_max[t.artist] = max(artist_max[t.artist], ranks[t.pid])

    out = {}
    for t in tracks:
        r = ranks[t.pid]
        f = fame.get(t.artist, 0)
        if r is None:          # artist not on Deezer: usually obscure, comedy or home recordings
            out[t.pid] = 0.1
        elif r == 0:           # not among the artist's top tracks
            out[t.pid] = 0.15 * f
        else:
            within = r / artist_max[t.artist]
            out[t.pid] = 0.45 * global_pct[t.pid] + 0.35 * within + 0.2 * f
    return out


def _weighted_pick(items: list, weights: list[float], k: int, rng: random.Random) -> list:
    """Weighted sampling without replacement (Efraimidis-Spirakis)."""
    keyed = [(rng.random() ** (1.0 / w), i) for i, w in enumerate(weights) if w > 0]
    keyed.sort(reverse=True)
    return [items[i] for _, i in keyed[:k]]


def _arrange(hits: list[Track], deep: list[Track], rng: random.Random) -> list[Track]:
    """Open strong, spread deep cuts out, avoid the same artist twice in a row."""
    out = list(hits)
    if deep:
        step = max(3, len(out) // (len(deep) + 1))
        for i, t in enumerate(deep):
            out.insert(min(len(out), 3 + i * (step + 1)), t)
    for i in range(1, len(out)):
        if out[i].artist == out[i - 1].artist:
            for j in range(i + 1, len(out)):
                if out[j].artist != out[i - 1].artist and (i + 1 >= len(out) or out[j].artist != out[i + 1].artist):
                    out[i], out[j] = out[j], out[i]
                    break
    return out


@dataclass
class Profile:
    """Your library minus the junk, plus how much you've been into each artist lately."""
    tracks: list[Track]
    vibes: dict[str, str]
    artist_aff: Counter


def listening_profile(tracks: list[Track], pop: Popularity, s: Settings,
                      history: list[dict]) -> Profile:
    now = datetime.now(timezone.utc)
    tracks = [t for t in tracks
              if t.is_song and t.present and not t.disliked and 45 <= t.duration <= 15 * 60]
    vibes = artist_vibes(tracks, pop.genre)
    tracks = [t for t in tracks if vibes[t.artist] not in ("_exclude",)]
    artist_aff = Counter()
    for t in tracks:
        artist_aff[t.artist] += track_affinity(t, now, history, s.recent_half_life_days)
    return Profile(tracks, vibes, artist_aff)


def build_mixes(prof: Profile, pop: Popularity, s: Settings,
                recently_mixed: set[str], seed: int | None = None) -> list[Mix]:
    now = datetime.now(timezone.utc)
    rng = random.Random(seed)
    tracks, vibes, artist_aff = prof.tracks, prof.vibes, prof.artist_aff
    hit = hit_scores(tracks, pop)

    by_vibe: dict[str, list[Track]] = defaultdict(list)
    for t in tracks:
        by_vibe[vibes[t.artist]].append(t)

    # Rank vibes by how much you've played them lately; big library sections break ties.
    vibe_score = {v: sum(math.sqrt(artist_aff[a]) for a in {t.artist for t in ts}) + len(ts) / 5000
                  for v, ts in by_vibe.items()}
    eligible = [v for v in by_vibe
                if v != "Unsorted" and v not in s.never_include and len(by_vibe[v]) >= s.min_pool]
    chosen = [v for v in s.always_include if v in by_vibe]
    chosen += [v for v in sorted(eligible, key=vibe_score.get, reverse=True) if v not in chosen]
    chosen = chosen[:s.mixes]

    fresh = freshness(now, recently_mixed)
    used: set[str] = set()
    mixes = []
    for vibe in chosen:
        pool = [t for t in by_vibe[vibe] if t.pid not in used]
        top = max((artist_aff[t.artist] for t in pool), default=0) or 1
        a_norm = {t.artist: artist_aff[t.artist] / top for t in pool}
        final, deep = assemble(pool, a_norm, hit, s, rng, fresh)
        used.update(t.pid for t in final)
        seeds = [a for a, _ in Counter({t.artist: a_norm[t.artist] for t in final}).most_common(3)]
        mixes.append(Mix(vibe, final, seeds, {t.pid for t in deep}))
    return mixes


def freshness(now: datetime, recently_mixed: set[str]):
    """Weight multiplier that rests songs from recent mixes, played today, or often skipped."""
    def fresh(t: Track) -> float:
        f = 0.3 if t.pid in recently_mixed else 1.0
        d = _days_ago(t.last_played, now)
        if d is not None and d < 1:
            f *= 0.6
        return f / (1 + 2 * t.skips / (t.plays + 1))
    return fresh


def assemble(pool: list[Track], a_norm: dict[str, float], hit: dict[str, float], s: Settings,
             rng: random.Random, fresh, caps: dict[str, int] | None = None,
             pinned: list[Track] = (), base: float = 0.25) -> tuple[list[Track], list[Track]]:
    """Pick and order one mix from pool. a_norm (0..1) says how central each artist is.

    caps overrides max_per_artist for particular artists; pinned tracks open the mix. base is how
    much peripheral artists (a_norm near 0) still get picked: lower keeps the mix more focused.
    Returns (ordered tracks, the deep cuts among them).
    """
    caps = caps or {}
    pool = [t for t in pool if t not in pinned]
    if not pool:
        return list(pinned), []
    # Top ~35% of the pool by hit score are "hits"; everything else is deep-cut territory.
    cut = sorted((hit[t.pid] for t in pool), reverse=True)[int(len(pool) * 0.35)]
    hits_pool = [t for t in pool if hit[t.pid] >= cut]
    deep_pool = [t for t in pool if hit[t.pid] < cut]
    size = s.tracks_per_mix - len(pinned)
    n_deep = round(size * s.deep_cut_ratio)
    n_hits = size - n_deep

    hw = [hit[t.pid] ** 2 * (base + a_norm[t.artist]) * fresh(t) * (1.5 if t.favorited else 1)
          for t in hits_pool]
    dw = [(base + 0.05 + a_norm[t.artist]) * (0.5 + hit[t.pid]) * fresh(t)
          * (1.5 if t.plays == 0 else 1 / (1 + 0.3 * t.plays))
          for t in deep_pool]

    per_artist: Counter = Counter(t.artist for t in pinned)

    def take(cands, weights, k, loosen=1):
        picked = []
        for t in _weighted_pick(cands, weights, len(cands), rng):
            if len(picked) == k:
                break
            if per_artist[t.artist] < caps.get(t.artist, s.max_per_artist) * loosen:
                per_artist[t.artist] += 1
                picked.append(t)
        return picked

    hits = take(hits_pool, hw, n_hits)
    deep = take(deep_pool, dw, n_deep + (n_hits - len(hits)))
    if len(hits) + len(deep) < size:     # small pool: loosen the per-artist cap
        chosen = {t.pid for t in hits + deep}
        rest = [(t, w) for t, w in zip(hits_pool, hw) if t.pid not in chosen]
        hits += take([t for t, _ in rest], [w for _, w in rest], size - len(hits) - len(deep),
                     loosen=2)
    # Lead with the biggest hit from the most central artist.
    hits.sort(key=lambda t: rng.random())
    if not pinned:
        lead = max(hits, key=lambda t: (a_norm[t.artist] > 0.5) * hit[t.pid], default=None)
        if lead:
            hits.remove(lead)
            hits.insert(0, lead)
    return list(pinned) + _arrange(hits, deep, rng), deep
