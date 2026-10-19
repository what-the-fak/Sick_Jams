"""Album picks: records by artists you don't own yet, chosen via the artists you play most.

Deezer lists related artists for each of your top recent artists. Artists that several of your
favourites point to rank highest. For each one we suggest the studio album holding most of their
popular songs.
"""

import math
from collections import defaultdict
from dataclasses import dataclass

from .mixer import Profile
from .popularity import Popularity, norm

SEED_ARTISTS = 25


@dataclass
class AlbumPick:
    artist: str
    title: str
    year: str
    link: str
    because: list[str]
    artist_id: int


def recommend_albums(prof: Profile, pop: Popularity, skip_ids: set[int], n: int = 5) -> list[AlbumPick]:
    owned = {norm(a) for a in prof.artist_aff}
    seeds = [a for a, _ in prof.artist_aff.most_common(SEED_ARTISTS * 2)
             if pop.artist(a).get("found")][:SEED_ARTISTS]
    if not seeds:
        return []
    top = prof.artist_aff[seeds[0]] or 1

    score: dict[int, float] = defaultdict(float)
    because: dict[int, list[tuple[float, str]]] = defaultdict(list)
    info: dict[int, dict] = {}
    for seed in seeds:
        w = prof.artist_aff[seed] / top
        for i, rel in enumerate(pop.related(seed)):
            if norm(rel["name"]) in owned or rel["id"] in skip_ids:
                continue
            s = w / (1 + 0.15 * i)              # closer matches count more
            score[rel["id"]] += s
            because[rel["id"]].append((s, seed))
            info[rel["id"]] = rel
    pop.save()

    # Recommended by several of your artists beats recommended strongly by one; a little fame
    # tips it toward records that are actually good.
    ranked = sorted(score, key=lambda i: score[i] * (1 + 0.05 * math.log1p(info[i]["fans"])),
                    reverse=True)

    picks: list[AlbumPick] = []
    per_seed: dict[str, int] = defaultdict(int)
    for aid in ranked:
        if len(picks) == n:
            break
        reasons = [s for _, s in sorted(because[aid], reverse=True)]
        if per_seed[reasons[0]] >= 2:           # don't let one favourite dominate the list
            continue
        album = pop.best_album(aid)
        if not album:
            continue
        per_seed[reasons[0]] += 1
        picks.append(AlbumPick(info[aid]["name"], album["title"], album["year"], album["link"],
                               reasons[:3], aid))
    pop.save()
    return picks


def format_picks(picks: list[AlbumPick], date: str) -> str:
    lines = [f"Sick Jams album picks ({date})", "Albums by artists not in your library.", ""]
    for i, p in enumerate(picks, 1):
        year = f" ({p.year})" if p.year else ""
        lines += [f"{i}. {p.artist} – {p.title}{year}",
                  f"   Because you play {', '.join(p.because)}",
                  f"   {p.link}", ""]
    return "\n".join(lines)
