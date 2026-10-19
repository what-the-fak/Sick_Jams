"""Collapses messy genre tags into a handful of vibes, resolved per artist.

Tags in a real library are all over the place ("Alternative & Punk", "AlternRock", blank...), so
each artist gets one vibe by voting across all of their tracks. Artists with no usable tags fall
back to the genre Deezer reports for them.
"""

import re
from collections import Counter, defaultdict

# (vibe, keywords). First vibe with a matching keyword wins; order matters.
# Keywords match whole words; a trailing * makes it a prefix ("alt*" matches "AlternRock").
RULES: list[tuple[str, tuple[str, ...]]] = [
    ("Ska & Reggae", ("ska", "reggae", "rocksteady")),
    ("Heavy", ("metal*", "hardcore", "post-hardcore", "screamo", "thrash", "grind*", "death", "doom",
               "djent", "sludge")),
    ("Punk & Emo", ("punk*", "emo", "oi!", "skate")),
    ("Hip-Hop", ("hip-hop", "hip hop", "rap", "trap", "grime")),
    ("Electronic", ("electro*", "techno", "house", "trance", "dance", "edm", "eurobeat", "initial d",
                    "drum & bass", "dubstep", "synth*", "idm")),
    ("Soundtracks & Anime", ("soundtrack*", "anime", "film*", "game*", "score", "cowboy bebop")),
    ("Roots & Blues", ("blues", "country", "folk", "americana", "bluegrass", "oldies", "jazz",
                       "soul", "motown", "acoustic", "singer", "celtic")),
    ("Alt & Indie", ("alt*", "indie", "grunge", "shoegaze", "post-rock", "britpop")),
    ("Rock", ("rock",)),
    ("Pop", ("pop", "r&b", "disco", "funk")),
]

# Tags that should never end up in a music mix.
EXCLUDE = ("comedy", "spoken", "audiobook", "podcast", "books", "speech", "holiday", "christmas",
           "kids", "children")

# Broad tags say little ("Rock" is on half the punk records out there), so a specific tag like
# "Ska" or "Punk Rock" outweighs several of them.
GENERIC = {"Rock": 0.3, "Alt & Indie": 0.3, "Pop": 0.3}

# Gracenote's catch-all tags: count them, but as a weak vote split across vibes.
SPLIT = {
    "alternative & punk": {"Alt & Indie": 0.5, "Punk & Emo": 0.5},
    "rock/pop": {"Rock": 0.5, "Pop": 0.5},
    "pop/rock": {"Rock": 0.5, "Pop": 0.5},
}


def vibe_of(tag: str) -> dict[str, float]:
    """Vibe votes for one raw tag; {} if unknown, {"_exclude": 1} if it should be dropped."""
    t = tag.lower().strip()
    if not t:
        return {}
    if any(x in t for x in EXCLUDE):
        return {"_exclude": 1.0}
    if t in SPLIT:
        return SPLIT[t]
    for vibe, keys in RULES:
        for k in keys:
            pat = re.escape(k.rstrip("*")) + ("" if k.endswith("*") else r"($|[^a-z])")
            if re.search(r"(^|[^a-z])" + pat, t):
                return {vibe: 1.0}
    return {}


def artist_vibes(tracks, deezer_genre_for) -> dict[str, str]:
    """Returns {artist: vibe}. deezer_genre_for(artist) -> raw genre str or None."""
    votes: dict[str, Counter] = defaultdict(Counter)
    for t in tracks:
        for v, w in vibe_of(t.genre).items():
            votes[t.artist][v] += w * GENERIC.get(v, 1.0)
    out = {}
    for artist in {t.artist for t in tracks}:
        c = votes.setdefault(artist, Counter())
        if sum(c.values()) < 3:     # thin evidence: let Deezer's genre weigh in
            dz = deezer_genre_for(artist)
            for v, w in (vibe_of(dz) if dz else {}).items():
                c[v] += 2 * w
        if c and c.get("_exclude", 0) >= sum(c.values()) / 2:
            out[artist] = "_exclude"
            continue
        if c:
            c.pop("_exclude", None)
        out[artist] = c.most_common(1)[0][0] if c else "Unsorted"
    return out
