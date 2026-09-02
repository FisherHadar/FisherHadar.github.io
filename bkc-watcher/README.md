# bkc-watcher

Watches Berkman Klein Center fellowship pages and tells you when a new call
for applications opens. Runs once a week, Monday morning. Python 3 standard
library only — nothing to install.

```
~/bkc-watcher/
├── check.py            the script
├── config.json         what to watch, and how  ← the file you edit
├── install.sh          install the weekly job
├── uninstall.sh        remove it
├── com.hadarfisher.bkc-watcher.plist.template   launchd schedule (macOS)
├── snapshots/          latest plain text per page, + archive/ of old versions
├── state.json          what was true last run (hashes, matched phrases)
├── run.log             every run: timestamp, page, changed yes/no
└── found.log           only the flagged events, in full
```

## Setup

```sh
cd ~/bkc-watcher
./install.sh                    # Monday 09:00
python3 check.py --force-notify # confirm notifications actually appear
python3 check.py                # first real run, writes the first snapshots
```

The first run writes a baseline snapshot per page and reports nothing (there
is nothing to compare against yet). Every run after that compares.

## What makes it shout

Two tiers, on purpose:

**Application signal** (the one worth interrupting you for) — the text matches
any of `now accepting`, `applications are open`, `open call`, `2027-2028`,
`2027-28`, `apply by`, `deadline`, *and* no longer contains
`this call for applications has now closed`.

Both halves matter. The fellowships page carries the heading "Open Call for
Fellowship Applications" permanently, even while the call is shut — so the
phrase alone would fire every single week and you would learn to ignore it.
The closed-marker check is what turns that into a real signal.

**Text changed** — any difference at all from the last snapshot. Always
recorded in `found.log`; notifies only if that page's `notify_on_change` is
true.

Heads up: `cyber.harvard.edu/news` changes most weeks by design, so it will
notify most weeks. If that gets annoying, set `"notify_on_change": false` on
the news entry in `config.json` — the keyword signal still gets through.

An identical alert is not repeated: once a page has been flagged, it stays
quiet until its content or its matched phrases actually change.

## Checking on it

```sh
python3 check.py --status     # what's watched, and the last known state of each
tail -20 ~/bkc-watcher/run.log
cat ~/bkc-watcher/found.log
```

`run.log` distinguishes the two things you care about:

```
... fellowships   OK      changed=no   signal=no   chars=43210  http=200   ← fine, nothing new
... fellowships   OK      changed=no   signal=yes  http=304  note=not-modified
... fellowships   ERROR   changed=unknown  signal=unknown  err=URLError: ... ← the script broke
```

`ERROR` or `WARN` means it could not check — not that nothing happened. If you
see nothing but `ERROR` for a few weeks, the site changed shape or your network
blocked it. `http=304` means the server confirmed the page is byte-identical
and sent no body at all.

## Changing the schedule

Re-run the installer with a new time; it replaces the old schedule rather than
adding a second one. Arguments are hour, minute, weekday (`0`=Sunday,
`1`=Monday … `6`=Saturday), local time.

```sh
./install.sh 7 30      # Monday 07:30
./install.sh 9 0 3     # Wednesday 09:00
./install.sh 8 15 1    # Monday 08:15
```

To edit by hand instead:

- **macOS** — `~/Library/LaunchAgents/com.hadarfisher.bkc-watcher.plist`, the
  `StartCalendarInterval` block. Then reload it:
  ```sh
  launchctl bootout gui/$(id -u)/com.hadarfisher.bkc-watcher
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hadarfisher.bkc-watcher.plist
  ```
- **Linux** — `crontab -e`, edit the `bkc-watcher/check.py` line.

If the Mac is asleep at the scheduled moment, launchd runs the job once when it
next wakes. It does not silently skip the week.

## Adding or removing a watched URL

Edit the `pages` list in `config.json`:

```json
{
  "name": "fellowships",
  "url": "https://cyber.harvard.edu/getinvolved/fellowships",
  "notify_on_change": true
}
```

- `name` labels it in the logs and names its snapshot file. Keep it short and
  filename-safe; changing it later orphans the old snapshot, which just means
  the next run treats the page as new.
- `notify_on_change` — `false` mutes the plain-diff tier for that page only.

To remove a page, delete its entry. Its snapshot stays in `snapshots/` doing
nothing; delete `snapshots/<name>.txt` too if you want it gone.

No restart needed — `config.json` is read fresh on every run.

To change the phrases themselves, edit `signal_phrases` and `closed_phrase` in
the same file. Match them lowercase; curly quotes, non-breaking spaces and
en-dashes are normalised before matching, so plain ASCII in the config is
correct even when the page uses typographic characters.

## Uninstalling

```sh
cd ~/bkc-watcher && ./uninstall.sh
```

Removes the schedule only. To delete everything:

```sh
rm -rf ~/bkc-watcher
```

## Being polite to Harvard's server

One request per page per run, once a week — about 12 requests a month. It
sends a real browser User-Agent (`user_agent` in `config.json`), waits 2–5
seconds between pages, and uses conditional GETs, so an unchanged page costs
the server a 304 with no body. Failures retry at most twice, backing off 5s
then 15s with jitter, and honour a `Retry-After` header if one comes back. A
4xx other than 429 is not retried at all.

Anything that goes wrong is written to `run.log` and the run continues to the
next page. The script always exits 0, so a network blip never turns into a
scheduler alarm.

## Troubleshooting

**No notification appeared.** Run `python3 check.py --force-notify`. If nothing
shows, check System Settings → Notifications → Script Editor (macOS shows
`osascript` notifications under the app that sent them) and make sure Do Not
Disturb is off. `found.log` is written either way, so nothing is lost.

**`command not found: python3`** — install Xcode Command Line Tools:
`xcode-select --install`.

**Everything suddenly reports `changed=yes`.** The site was redesigned. Read
`snapshots/<name>.txt` to see what the script now sees; if it is nav-bar noise,
add a regex to `ignore_line_patterns` in `config.json`.

**Verify the detection rules still work:** `python3 check.py --self-test` runs
the whole pipeline against built-in fixtures and asserts the outcomes. It
touches no real snapshot.

**Start over:** `python3 check.py --reset` clears snapshots and state, keeping
the logs. The next run rebuilds the baseline.
