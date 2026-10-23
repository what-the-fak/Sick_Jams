"""Reads your library and writes playlists.

macOS: Music.app through JavaScript for Automation (osascript).
Windows: iTunes through its COM interface, driven by PowerShell (no extra installs needed).
"""

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

WINDOWS = sys.platform == "win32"

_READ_SCRIPT = r"""
const lib = Application("Music").libraryPlaylists[0];
const ft = lib.fileTracks;
const f = ["persistentID", "name", "artist", "albumArtist", "album", "genre", "playedCount",
           "playedDate", "skippedCount", "dateAdded", "duration", "favorited", "disliked", "year",
           "mediaKind"];
const cols = {};
for (const k of f) cols[k] = ft[k]();
let loc;
try { loc = ft.location(); } catch (e) { loc = null; }
const out = [];
for (let i = 0; i < cols.persistentID.length; i++) {
  const r = {};
  for (const k of f) {
    const v = cols[k][i];
    r[k] = (v instanceof Date) ? v.getTime() / 1000 : v;
  }
  r.present = loc ? !!loc[i] : true;
  out.push(r);
}
JSON.stringify(out);
"""


@dataclass
class Track:
    pid: str
    name: str
    artist: str
    album: str
    genre: str
    plays: int
    last_played: datetime | None
    skips: int
    added: datetime | None
    duration: float
    favorited: bool
    disliked: bool
    year: int
    is_song: bool
    present: bool


def _ts(v):
    return datetime.fromtimestamp(v, timezone.utc) if v else None


def _osascript(script: str, *args: str) -> str:
    if sys.platform != "darwin":
        raise RuntimeError("Music.app scripting needs macOS.")
    res = subprocess.run(["osascript", "-l", "JavaScript", "-e", script, *args],
                         capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"Music.app scripting failed: {res.stderr.strip()}")
    return res.stdout


def read_library() -> list[Track]:
    if WINDOWS:
        print("  (iTunes on Windows reads one song at a time; this can take a minute or two)")
        rows = json.loads(_powershell(_WIN_READ) or "[]")
        if isinstance(rows, dict):      # PowerShell un-wraps one-item arrays
            rows = [rows]
    else:
        rows = json.loads(_osascript(_READ_SCRIPT))
    tracks = []
    for r in rows:
        tracks.append(Track(
            pid=r["persistentID"],
            name=r["name"] or "",
            artist=(r["artist"] or r["albumArtist"] or "").strip(),
            album=r["album"] or "",
            genre=(r["genre"] or "").strip(),
            plays=r["playedCount"] or 0,
            last_played=_ts(r["playedDate"]),
            skips=r["skippedCount"] or 0,
            added=_ts(r["dateAdded"]),
            duration=r["duration"] or 0,
            favorited=bool(r["favorited"]),
            disliked=bool(r["disliked"]),
            year=r["year"] or 0,
            is_song=r["mediaKind"] == "song",
            present=r["present"],
        ))
    return tracks


# Playlists are kept in one folder and reused by slot ("Sick Jams 1 ...", "Sick Jams 2 ...") so
# their identity survives across runs and your phone sync selection keeps working.
_WRITE_SCRIPT = r"""
function run(argv) {
  const spec = JSON.parse(argv[0]);
  const m = Application("Music");
  const lib = m.libraryPlaylists[0];

  // (m.folderPlaylists() errors when there are none, so filter all playlists instead)
  let folder = m.playlists().find(p => p.class() === "folderPlaylist" && p.name() === spec.folder);
  folder = folder || m.make({new: "folderPlaylist", withProperties: {name: spec.folder}});
  const folderId = folder.persistentID();

  const existing = {};
  for (const p of m.userPlaylists()) {
    let parentId = null;
    try { parentId = p.parent.persistentID(); } catch (e) {}
    if (parentId !== folderId) continue;
    const mt = p.name().match(/^Sick Jams (\d+|Bonus|Claude)\b/);
    if (mt) existing[mt[1]] = p;
  }

  const report = [];
  spec.mixes.forEach(mix => {
    const slot = mix.slot;
    let p = existing[slot];
    if (p) { delete existing[slot]; m.delete(p.tracks); }
    else p = m.make({new: "userPlaylist", at: folder, withProperties: {name: mix.name}});
    p.name = mix.name;
    try { p.description = mix.description; } catch (e) {}
    let added = 0;
    for (const id of mix.ids) {
      const hit = lib.tracks.whose({persistentID: id});
      if (hit.length) { hit[0].duplicate({to: p}); added++; }
    }
    report.push(mix.name + ": " + added + " tracks");
  });
  // Daily slots you no longer need go away; Bonus/Claude mixes stay until replaced.
  for (const slot in existing) {
    if (slot === "Bonus" || slot === "Claude") continue;
    m.delete(existing[slot]); report.push("removed old slot " + slot);
  }
  return report.join("\n");
}
"""


def write_playlists(folder: str, mixes: list[dict]) -> str:
    """mixes: [{"slot": "1".."N", "Bonus" or "Claude", "name", "description", "ids": [...]}]"""
    spec = json.dumps({"folder": folder, "mixes": mixes})
    if WINDOWS:
        return _powershell(_WIN_WRITE, spec).strip()
    return _osascript(_WRITE_SCRIPT, spec).strip()


# ---------------------------------------------------------------------------------------------
# Windows: iTunes COM via PowerShell. Same behaviour as the scripts above: persistent IDs are the
# 64-bit high/low pair as 16 hex digits, playlists live in one folder and are reused by slot.

_WIN_COMMON = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$app = New-Object -ComObject iTunes.Application
function Get-Pid($o) {
  '{0:X8}{1:X8}' -f $app.ITObjectPersistentIDHigh($o), $app.ITObjectPersistentIDLow($o)
}
"""

_WIN_READ = _WIN_COMMON + r"""
$epoch = [datetime]'1970-01-01'
function To-Unix($d) {
  if ($d -eq $null -or $d.Year -lt 1971) { return 0 }
  return [long]($d.ToUniversalTime() - $epoch).TotalSeconds
}
$tracks = $app.LibraryPlaylist.Tracks
$out = New-Object System.Collections.Generic.List[object]
for ($i = 1; $i -le $tracks.Count; $i++) {
  $t = $tracks.Item($i)
  if ($t.Kind -ne 1) { continue }            # 1 = file track
  $rating = [int]$t.Rating                    # 0-100; 4-5 stars = favourite, 1 star = disliked
  $out.Add([ordered]@{
    persistentID = (Get-Pid $t)
    name = $t.Name; artist = $t.Artist; albumArtist = $t.AlbumArtist; album = $t.Album
    genre = $t.Genre; playedCount = $t.PlayedCount; playedDate = (To-Unix $t.PlayedDate)
    skippedCount = $t.SkippedCount; dateAdded = (To-Unix $t.DateAdded)
    duration = $t.Duration; year = $t.Year
    favorited = ($rating -ge 80); disliked = ($rating -gt 0 -and $rating -le 20)
    mediaKind = $(if ($t.VideoKind -eq 0 -and -not $t.Podcast) { 'song' } else { 'other' })
    present = [bool]$t.Location
  })
}
ConvertTo-Json -InputObject $out -Compress -Depth 3
"""

_WIN_WRITE = _WIN_COMMON + r"""
$spec = Get-Content -Raw -Encoding UTF8 $args[0] | ConvertFrom-Json
$lib = $app.LibraryPlaylist
$all = $app.LibrarySource.Playlists

$folder = $null
for ($i = 1; $i -le $all.Count; $i++) {
  $p = $all.Item($i)
  if ($p.Kind -eq 2 -and $p.SpecialKind -eq 4 -and $p.Name -eq $spec.folder) { $folder = $p }
}
if (-not $folder) { $folder = $app.CreateFolder($spec.folder) }
$folderId = Get-Pid $folder

$existing = @{}
for ($i = 1; $i -le $all.Count; $i++) {
  $p = $all.Item($i)
  if ($p.Kind -ne 2 -or $p.SpecialKind -eq 4) { continue }
  $parent = $null
  try { $parent = $p.Parent } catch {}
  if (-not $parent -or (Get-Pid $parent) -ne $folderId) { continue }
  if ($p.Name -match '^Sick Jams (\d+|Bonus|Claude)\b') { $existing[$Matches[1]] = $p }
}

$report = @()
foreach ($mix in $spec.mixes) {
  $p = $existing[$mix.slot]
  if ($p) {
    $existing.Remove($mix.slot)
    while ($p.Tracks.Count -gt 0) { $p.Tracks.Item(1).Delete() }
  } else {
    $p = $folder.CreatePlaylist($mix.name)
  }
  $p.Name = $mix.name
  $added = 0
  foreach ($id in $mix.ids) {
    $hi = [Convert]::ToInt32($id.Substring(0, 8), 16)
    $lo = [Convert]::ToInt32($id.Substring(8, 8), 16)
    $t = $lib.Tracks.ItemByPersistentID($hi, $lo)
    if ($t) { [void]$p.AddTrack($t); $added++ }
  }
  $report += "$($mix.name): $added tracks"
}
# Daily slots you no longer need go away; Bonus/Claude mixes stay until replaced.
foreach ($slot in @($existing.Keys)) {
  if ($slot -eq 'Bonus' -or $slot -eq 'Claude') { continue }
  $existing[$slot].Delete(); $report += "removed old slot $slot"
}
$report -join "`n"
"""


def _powershell(script: str, payload: str | None = None) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        ps1 = Path(tmp) / "sick_jams.ps1"
        ps1.write_text(script, encoding="utf-8-sig")   # BOM so Windows PowerShell reads UTF-8
        args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)]
        if payload is not None:
            spec = Path(tmp) / "spec.json"
            spec.write_text(payload, encoding="utf-8")
            args.append(str(spec))
        res = subprocess.run(args, capture_output=True)
    out = res.stdout.decode("utf-8", errors="replace").lstrip("\ufeff")
    if res.returncode != 0:
        err = res.stderr.decode("utf-8", errors="replace").strip()
        if "iTunes.Application" in err or "80040154" in err:
            err = "iTunes for Windows doesn't seem to be installed (the Apple Music app can't be scripted)."
        raise RuntimeError(f"iTunes scripting failed: {err}")
    return out
