# Sick Jams

Spotify-Daily-Mix-style playlists for your own music library, built from what you've been
playing lately. Works with Music.app on a Mac and iTunes on Windows.

**Run it:** double-click `Sick Jams.command` (Mac) or `Sick Jams.bat` (Windows), then sync your
phone.

## Setup

You need a local music library, meaning song files you own. It doesn't work with Apple Music
streaming-only libraries.

- **Mac:** uses the Music app.
- **Windows:** uses [iTunes for Windows](https://www.apple.com/itunes/). The newer "Apple Music"
  app for Windows can't be scripted, so it won't work. Windows support is new and hasn't been
  tested on a real PC yet. Please open an issue if something breaks.

1. **Python 3.11 or newer.** Check with `python3 --version` (Mac) or `py --version` (Windows).
   Install from [python.org](https://www.python.org/downloads/) if needed. On Windows, tick **Add
   python.exe to PATH** in the installer. Nothing else is needed; it only uses the standard
   library.
2. **Get the code:** `git clone https://github.com/what-the-fak/Sick_Jams.git`, or use the green
   **Code → Download ZIP** button and unzip it.
3. **Run the launcher.** On the first run:
   - **Mac:** if you downloaded the ZIP, macOS may block `Sick Jams.command`. Right-click it →
     **Open** once. macOS then asks to let Terminal control Music. Click **OK**.
   - **Windows:** if SmartScreen warns about `Sick Jams.bat`, click **More info → Run anyway**.
     iTunes opens on its own if it isn't already running.
   - It looks up every artist in your library on Deezer, which takes a few minutes. Later runs take
     seconds.
4. **Optional, Claude's pick:** see [below](#claudes-pick). It needs `pip3 install anthropic` and
   an Anthropic API key.

Playlists land in a **Sick Jams** folder in Music or iTunes as `Sick Jams 1 · Punk & Emo`,
`Sick Jams 2 · Rock`, and so on. The same playlists are refilled each run (never duplicated). Tick
the folder once in your phone's sync settings (Finder on a Mac, iTunes on Windows), and every
later run syncs with no extra steps.

## How the mixes are picked

1. **Vibes.** Genre tags get grouped into ~10 vibes. Each artist gets one vibe, voted across all
   their tracks, so mis-tagged or untagged songs still land in the right mix. Artists with no tags
   use the genre Deezer lists for them.
2. **What you're into lately.** Recent plays count most and fade with a 14-day half-life. Each run
   also records how many plays happened since the last run, so recent-listening data improves the
   more you use it. The vibes you've played most recently get mixes. Each mix leans on the artists
   you've played most in that vibe.
3. **Hits first.** Deezer's public API (free, no account) gives each artist's top tracks and a
   popularity score. About 80% of every mix comes from the top of that list, and the rest is deep
   cuts: lesser-known songs, mostly ones you haven't played, by artists you like or big-name artists.
4. **Variety.** Songs from the previous two runs, songs played today, and songs you often skip are
   less likely to come up. There are at most 4 songs per artist, the same artist never plays twice
   in a row, and no song appears in two mixes.

## Bonus mix

When you run it, it asks what you're into right now. Type a band, album or song (`Rancid`,
`...And Out Come the Wolves`, `Basket Case`, `Alkaline Trio - Radio`) to also get
**Sick Jams Bonus · <that>**, or just press Enter to skip.

- It checks your library first (artist, then album, then song) and falls back to Deezer, so it
  also works for bands you don't own yet.
- The mix opens with the song you named, or the biggest hit from the band or album.
- The rest is mostly Deezer's related artists that are in your library, topped up from the same
  vibe. It only uses songs from your library, so everything syncs.
- Pressing Enter keeps your last bonus mix. A new one replaces it.

From the command line, use `--into "Rancid"` to skip the question, or `--no-prompt` to never ask.

## Album picks

Each run also writes **`Album Picks.txt`**: 5 albums by artists who aren't in your library at all.
Deezer lists similar artists for each of your 25 most-played recent artists. An artist ranks higher
the more of your favorites point to them, and closer matches count more. For each pick it chooses
the studio album holding most of that artist's popular songs, skipping best-ofs, live albums and
singles. Each pick lists which of your artists it's based on and has a Deezer link. Picks don't repeat for 4
weeks, so the list changes each run. Set `album_picks = 0` in `config.toml` to turn it off.

## Claude's pick

If you add an Anthropic API key, each run also asks Claude Haiku for something new. It gets:
- your recent plays
- the mixes that run just made, so it steers somewhere different
- the artists in your library
- a numbered list of candidate songs you own

Back come **Sick Jams Claude · <theme>**, a ~30-song playlist from your library with a short
explanation in its description, plus **2 album suggestions** added to `Album Picks.txt` under
"Claude's picks". Every suggested album is checked against Deezer and then MusicBrainz, so made-up
albums are dropped, and albums you already own are skipped. If a run doesn't call Claude, the last
Claude playlist stays put.

**Cost:** the prompt is about 20K tokens. With Haiku 5.5's thinking included, one run costs about
half a cent, so $5 of credit lasts about 800 runs. The terminal prints the actual cost each run.

**Setup:**
1. `pip3 install anthropic` (Windows: `py -m pip install anthropic`).
2. Get an API key. A Claude Pro subscription doesn't cover API use; it's billed separately.
   1. Sign in at [platform.claude.com](https://platform.claude.com) (the Claude Console).
   2. Add a little prepaid credit under **Billing**.
   3. Create a key under **API Keys**.
3. Run `python3 -m sick_jams --set-api-key` and paste the key. It's checked, then saved in `.data/`,
   which git never uploads. Setting an `ANTHROPIC_API_KEY` environment variable also works.

Use `--no-claude` to skip it for one run, or set `enabled = false` under `[claude]` in
`config.toml` to turn it off.

## Command line

```
python3 -m sick_jams --dry-run     # preview in the terminal, don't change Music
python3 -m sick_jams --no-lookup   # skip Deezer (uses cached data) and Claude
python3 -m sick_jams --no-claude   # skip Claude's pick this run
python3 -m sick_jams --set-api-key # save an Anthropic API key for Claude's pick
```

Needs Python 3.11+ on macOS or Windows. The first run looks up each artist on Deezer, which takes a few
minutes. The results are cached in `.data/` and refreshed every 60 days. After that, a run takes
a few seconds.
