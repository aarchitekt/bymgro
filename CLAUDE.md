# bymgro — project notes for Claude

## "Dev" vs "Main" — terminology the user set explicitly (same as memori-mvp)

- **"Dev"** = the locally-hosted version on the user's own Mac. Default place
  to work. Iterate here freely.
- **"Main"** = the public, deployed version on Railway.

**Stay in dev by default.** Do not `git push` / trigger a Railway deploy
unless the user explicitly says to, even if something looks tested and ready
locally. The user commits and pushes themselves — see "Deployment" below.

## Local dev on the user's Mac

- Local clone lives at `~/Documents/bymgro` on the user's Mac, with its own
  `.venv`.
- Start command (run by the user in their own Mac Terminal):
  `.venv/bin/uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000`
  — `--host 0.0.0.0` so it's reachable from the user's phone on the same
  WLAN at `http://<mac-lan-ip>:8000`.
- The device-bridge shell (`device_bash`) available to a cloud Cowork session
  is an isolated Linux VM with no network access — it can read/write files
  in the mounted folder but cannot run the Mac's own Python/uvicorn. Don't
  try to start the server through the bridge; only the user's own Mac
  Terminal can run it.
- The bridge also cannot delete files (confirmed for both `rm` and
  `os.remove()`) — if something needs removing, move it into a
  `_to_delete/` folder instead and tell the user, don't fight the
  restriction.

## Deployment (only once the user asks for it)

This Cowork cloud sandbox cannot push to GitHub directly (fixed security
boundary, not a credentials problem — do not try to work around it with a
token). The device-bridge shell to the user's Mac has no network access
either. So Claude cannot push code to GitHub from either side.

The flow once the user is ready to go live:
1. The user runs `git add -A && git commit -m "..." && git push` themselves
   in their own Mac Terminal.
2. They tell Claude, who triggers a Railway redeploy via the Railway MCP
   tools (if connected in that session) — or Railway's own auto-deploy on
   push picks it up automatically once the GitHub repo is linked.

## Versioning rule

Every future update should bump `APP_VERSION` in `frontend/index.html` AND
`backend/main.py` (currently `"1.3"`) — shown in Einstellungen as a small
muted "bymgro v1.3" line at the very bottom (`#app-version-line`, styled
like memori-mvp's own version tag) — bump as part of the change, before
syncing/deploying.

## Data model / where things live

- `backend/storage.py` — all SQLite access, now multi-user (see "Identity"
  below). Tables: `users`, `profile`, `bodyweight_log`, `plan_exercises`
  (the editable push/pull plan), `workout_sessions`, `session_sets`,
  `nutrition_log`, `supplements`, `supplement_log`, `habit_log`,
  `friendships`, `user_achievements`. Every table except `users` itself is
  scoped by `user_id`.
- The very first user ever created on a fresh `bymgro.db` inherits history
  (same "first account inherits demo session" pattern as memori-mvp) —
  every user after that starts with the canonical empty plan
  (`storage.CANONICAL_PLAN`), no history. This only happens once (checked
  via `users` table being empty at creation time); it will NOT re-seed on
  later restarts. To reset local dev data, delete `bymgro.db` (not
  `seed_data.json`) and restart.
- That first-user seed prefers **live local history over the static seed
  file** when both are available: `storage._seed_from_legacy_archive()`
  checks for `*_legacy_pre_1_1` tables (see "Legacy schema migration"
  below) and, if present, copies real workout_sessions/plan_exercises/
  bodyweight_log/profile rows into the new schema — including
  `workout_sessions.id`, preserved on purpose so `session_sets` (never
  renamed by the migration, only ALTER'd) keeps pointing at the right rows
  without needing a rewrite. Falls back to `backend/seed_data.json` only
  when no legacy archive exists (a genuinely fresh install).

## Legacy schema migration (Update 1.1)

Update 1.1 moved from a single-profile schema to multi-user. On startup,
`storage._migrate()` detects an older `bymgro.db` (its `profile` table
lacks `user_id`) and renames `profile`/`plan_exercises`/`workout_sessions`/
`bodyweight_log` to `*_legacy_pre_1_1` before recreating the new tables —
nothing is deleted. **Caught in the wild once already**: if the old app is
still running live (via `--reload`) while new code lands on disk, a
workout logged *between* the last old-schema restart and the next reload
ends up archived along with the rest of the old data — the fix is the
legacy-archive seeding above, which reattaches it to the first new-schema
user instead of losing it under the static seed file. If this class of bug
ever resurfaces (e.g. a *second* schema migration in the future racing
with a live session), the same pattern applies: prefer copying forward
from whatever `*_legacy_pre_N` tables exist over any static snapshot.

`next_day_type()` alternates push/pull based on the most recently
*finished* session for that user — the user can override it manually
before starting.

## FOOTGUN: renaming a table auto-rewrites OTHER tables' FK clauses (found + fixed in Update 1.2)

`_archive_legacy_single_user_schema()`'s `ALTER TABLE workout_sessions
RENAME TO workout_sessions_legacy_pre_1_1` has a SQLite side effect that
isn't obvious: SQLite auto-rewrites any FOREIGN KEY clause in *other*
tables that referenced the renamed table, repointing it at the new name.
`session_sets` was never itself renamed (only ALTER'd to add `logged`), so
its `REFERENCES workout_sessions(id)` got silently repointed at
`workout_sessions_legacy_pre_1_1(id)` the moment the archival ran. Every
session created *after* that (i.e. any real workout logged post-Update-1.1)
lives in the new `workout_sessions` table, so the FK never matched and
`log_set()` raised `sqlite3.IntegrityError: FOREIGN KEY constraint failed`
on every single set. Caught via a Playwright smoke test while building
Update 1.2 (start a workout → log a set → "Konnte nicht speichern" toast),
not by the user hitting it live — but their real Mac DB had this exact
broken FK sitting in it already, from the original Update 1.1 migration.

Fix: `storage._fix_session_sets_fk()`, called from `_migrate()` on every
startup. It detects a `session_sets` table whose own `sqlite_master.sql`
still mentions `workout_sessions_legacy_pre_1_1` and rebuilds the table
(rename aside → CREATE with the FK correctly pointed at the live
`workout_sessions` → copy all rows back in, preserving `id` → drop the
temp table). Existing data is untouched, only the constraint target
changes. Idempotent and self-healing — runs once per DB, then the SQL no
longer matches so it's a no-op on every later boot. **If any future schema
change renames a table that something else has a FK pointing at, check for
this exact failure mode again** — it's a general SQLite gotcha, not
specific to this one migration.

## Identity / multi-user (Update 1.1)

No passwords — deliberately low-friction, matching a fast personal-use gym
app. On first load the frontend generates a `crypto.randomUUID()`, stores it
in `localStorage.bymgro_user_id`, and sends it as the `X-User-Id` header on
every API call (`backend/main.py`'s `uid()` helper lazily creates the user
row via `storage.get_or_create_user()` if it doesn't exist yet — there's no
separate signup step). This id is the bearer credential; there's no
recovery if `localStorage` is cleared (acceptable at this scale, same
trade-off memori-mvp made with its `session_id`).

Separately, every user gets a short human-shareable `code` (6 chars,
`storage.gen_code()`) for the **social/friends** feature — that's what you
type into a friend's app to connect, not the internal UUID. Friendships are
mutual (`storage.add_friend()` inserts both directions).

## Gamification (Update 1.1)

- **XP/level**: computed on the fly in `storage.gamification_status()`, not
  stored — `xp = sessions*15 + sets*1 + nutrition_days*3 + habit_days*2 +
  achievements*20 + streak_windows*8`, mapped to a level via
  `LEVEL_THRESHOLDS`. Recomputing from raw counts avoids any
  incremental-update sync bugs.
- **Workout streak** ("constant grow"): `storage.workout_streak()` counts
  consecutive backward-looking 2-day windows that each contain at least one
  finished session — i.e. it tolerates a rest day between workouts but
  breaks on two in a row. `stage` (0–5) is `windows // 2`, meant to drive a
  future visual growth metaphor (flame/plant) if that gets built out
  further; currently just shown as a flame icon + text in the Profil level
  card.
- **Clean streaks** (habits): `storage.clean_streaks()` — days since the
  last day any of alcohol/smoke/drugs was checked true, per category plus a
  combined one. Falls back to account-creation date if never logged.
- **Achievements**: static list in `storage.ACHIEVEMENTS`
  (id/name/desc/icon), unlock conditions are a plain dict of booleans inside
  `gamification_status()` — add a new achievement by adding one entry to
  both. `user_achievements` just records id + timestamp; unlock checks run
  on every `/api/gamification` call (also called after finishing a
  workout), newly-unlocked ones come back in `newly_unlocked` and the
  frontend queues them into the celebration modal (`showUnlockQueue` /
  `#unlock-modal`).

## Nutrition & habits (Update 1.1)

- `nutrition_log` is one row per user per day (calories, protein_g); the
  Ernährung screen computes protein-per-kg client-side from
  `profile.weight_kg`, live as you type.
- `supplements` is a user-defined list (name only); `supplement_log` is a
  per-day taken/not-taken join row, toggled from the Ernährung screen.
- `habit_log` is one row per user per day with three booleans
  (alcohol/smoke/drugs); the Habit-Tracker screen also renders a 30-day
  strip (`renderCalStrip`) — orange = a flagged day, green = clean.

## Design language

Deliberately monochrome/minimal like the sibling app (memori-mvp): CSS custom
properties `--bg`/`--ink`/`--muted`/`--panel`/`--line` swap between light and
dark via `html[data-theme="dark"]` (or no attribute for light), controlled
from the Einstellungen screen — see "Explicit dark/light mode chips" below
for how (Update 1.3 replaced a single ambiguous toggle button with this).
One
accent color (`--accent`, orange) is used sparingly — set-pills once logged,
chart lines, the timer hand, XP bar — never as a second UI color; `--good`
(green) is reserved for "clean"/positive habit state only. Keep any new
screen to that same restraint: one primary action visible at a time.

**All icons are hand-drawn inline SVGs** (`ICONS` object near the top of
`frontend/index.html`'s script, `stroke="currentColor"`, no fill) — there
are deliberately zero emoji anywhere in the UI (Update 1.1 removed the
original 🏋️⏱️📈👤 tab icons etc. for this reason). Add new icons to that
same object rather than reaching for an emoji or an external icon font.

## Progress screen: full-screen swipeable carousel (Update 1.1)

`renderProgress()` builds one full-height `.progress-page` per exercise
(bodyweight always first) inside `#progress-carousel`, which uses CSS
`scroll-snap-type: y mandatory` — vertical swipe/scroll between exercises is
native browser scrolling, not hand-rolled touch math (unlike the timer).
Each page is one big line+area chart (`buildBigChart()`) with real dates on
the x-axis (first/middle/last labelled). Re-rendered fresh every time the
tab is opened (`showScreen()` calls `renderProgress()` for
`progress-screen`), so it always reflects the latest sets.

## Workout screen navigation (Update 1.1)

Per user feedback, the old full-width "Zurück"/"Weiter" buttons are gone —
navigation between exercises is two small icon-only chevron buttons flanking
the dot progress row (`#prev-ex-btn`/`#next-ex-btn`). Set pills are now a
toggle, not a one-way action: tapping a already-logged pill un-logs it
(`toggleSet()` sends `logged:false`, which `storage.get_progress()` and
`get_history()` filter out via `ss.logged=1` — an unchecked set stays in
`session_sets` but is invisible everywhere else). The muscle-group label and
secondary buttons were deliberately shrunk/moved to a small corner
icon-cluster (rest-timer + finish) to keep the active-exercise view as
uncluttered as possible.

## Timer winding direction (Update 1.1 fix, superseded by Update 1.3 rework)

The Update 1.1 fix just flipped the sign of an angle-delta calculation.
That turned out to be an incomplete fix: angle-delta tracking is inherently
position-dependent — "dragging left" flips the sign of the resulting
rotation depending on whether the touch starts near the top or bottom of
the dial — so the direction could still feel wrong depending on where on
the dial the user grabbed it, and the user reported it was still backwards
in Update 1.3. See the Update 1.3 section below for the actual fix
(horizontal-drag tracking + a reworked countdown sweep direction).

## Timer wind + countdown direction rework (Update 1.3 fix)

Two distinct, previously-conflated problems, both now fixed:

1. **Winding was position-dependent.** Replaced angle-delta tracking with
   plain horizontal-drag tracking: `onMove()` now computes `dx = p.x -
   lastX` and does `accumDeg -= dx * DEG_PER_PX` — dragging the finger left
   always increases the wound time, dragging right always decreases it,
   regardless of where on the dial the touch started. `lastX` is set from
   `onDown()`; the old `angleOf()` helper is gone.
2. **The countdown always swept counterclockwise, independent of winding
   direction.** Per explicit user request ("mit dem finger nach links
   aufziehen und sie dann nach rechts laufen soll"), the countdown now
   sweeps *clockwise*, continuing smoothly from wherever the hand was at
   the moment of release and landing exactly on 12 o'clock when it hits
   zero. `startCountdown()` captures `windPhiAtRelease` (the hand's
   normalized angle at release) and `sweepTotalAtRelease = (360 - phi) +
   360*laps`; `tick()` draws
   `normDeg(windPhiAtRelease + progress * sweepTotalAtRelease)` each frame,
   where `progress = elapsed / runDurationSeconds`. Verified via Playwright
   (mouse-drag simulation + screenshot angle math): three sampled frames at
   `0:13 → 0:11 → 0:08` moved through clock-angle `74° → 123° → 189°`,
   monotonically increasing — confirmed clockwise and matching the expected
   sweep-formula output within a few degrees.

If winding or sweep direction ever needs flipping again: winding sign is
the one `accumDeg -=` line in `onMove()`; sweep direction is the sign in
`sweepTotalAtRelease`'s formula in `startCountdown()`. Don't conflate the
two again — they're independent bugs that happen to both show up as "the
timer runs backwards."

## Layout: Memori-style fixed icons instead of a bottom tabbar (Update 1.2)

The old 5-tab `nav#tabbar` is gone. Navigation now mirrors memori-mvp's own
single-stage layout (see `~/Downloads/memori-mvp/frontend/index.html`'s
`#graph-btn`/`#diary-btn`/`#camera-btn`/`#settings-btn` for the pattern this
was copied from): small icon buttons (`.mem-btn`) are `position: fixed`
directly inside each `<section class="screen">`, so they never scroll with
that screen's content and simply disappear when the screen is
`display:none` — no extra JS toggling needed.

On the Workout screen (home/center, matches memori's stage):
- top-left corner = Plan/Anpassen (`#plan-edit-btn`, matches memori's Reflect
  corner)
- top-right corner = Fortschritt (`#open-progress-btn`, matches Compose)
- top-center, small+muted = Timer (`#open-timer-btn`, matches memori's
  camera-shortcut spot)
- bottom-center, two small+muted icons side by side in `.bottom-pair-wrap`
  = Sozial (`#open-social-btn`) + Einstellungen (`#open-settings-btn`) —
  **both currently open the same `#settings-screen`** (per explicit user
  request, "das sollte vorerst vielleicht die selbe Seite sein"); Sozial
  additionally scrolls down to `#settings-social-section` after opening.
  Splitting them into genuinely separate screens later just means moving
  `open-social-btn`'s listener to its own `showScreen(...)` call.

Every other screen (Timer/Fortschritt/Einstellungen/Plan) has exactly one
fixed top-left `.mem-btn.corner.left` "back" button (chevBack icon) that
returns to `workout-screen`. A `.page-content` wrapper (`padding-top: 46px`)
sits inside each screen so its real content clears the fixed icons instead
of rendering underneath them; `.progress-page` (inside the carousel) got
the same top-padding treatment directly since it isn't inside a
`.page-content` wrapper.

Gotcha hit while building this: `.bottom-pair-wrap` centers itself with
`left:50%; transform:translateX(-50%)`, and CSS transforms create a new
containing block for `position:fixed` descendants — so the two `.mem-btn`
icons inside it were resolving their fixed position relative to the wrap,
not the viewport, and (with no offsets of their own) collapsed on top of
each other instead of respecting the flex `gap`. Fixed with `.bottom-pair-wrap
.mem-btn { position: static; }` so flexbox actually lays them out; only the
wrapper itself needs to be fixed.

## Settings / Sozial merged screen (Update 1.2)

`#profile-screen` and `#social-screen` were merged into one `#settings-screen`
(profile fields, dark mode, version tag, quick-nav to Ernährung/Habits/
Erfolge, AND the friend-code/add-friend/friends-list content all in one
scrollable page) — see "Layout" above for why. All the original element ids
(`#p-name`, `#my-code`, `#friends-list`, etc.) are unchanged, just relocated
in the markup, so `loadProfile()`/`loadSocial()`/etc. needed no changes
beyond `showScreen("settings-screen")` replacing the two old screen ids
(`nutrition-close-btn`/`habits-close-btn`/`achievements-close-btn` all
return here too now).

## Goal chips instead of a free-text field (Update 1.2)

"Ziel" on the profile is now a 2×2 grid of selectable presets (`GOALS` array
in the script, currently Skinny Fat weg / Shredded werden / Crazy Bulk /
Team Condi) instead of a `<textarea>`. `backend/storage.py`'s
`profile.goal` column is still plain `TEXT` — no migration was needed, the
frontend just writes one of the preset ids as the string. Loading a profile
whose `goal` doesn't match any current preset id (e.g. old free-text from
before Update 1.2) just leaves no chip selected, which is a safe no-op, not
an error — add a new preset by adding one entry to `GOALS`.

## Progress calendar page (Update 1.2)

`renderProgress()`'s carousel now always starts with a calendar page
(`calendarPageHtml()` / `renderCalendarBody()`) before the bodyweight/
exercise chart pages, built from `/api/history?limit=1000` (finished
sessions only, keyed by date → day_type). Month navigation is tap
prev/next arrows (`#cal-prev`/`#cal-next`), not swipe, since vertical swipe
inside `.progress-carousel` is already claimed by the page-to-page
scroll-snap. `calYear`/`calMonth` are module-level state so the selected
month survives re-opening Fortschritt within the same session (resets to
the current month only on a fresh page load).

## Timer implementation

`frontend/index.html`'s IIFE at the bottom (search "wind-up egg timer") draws
the dial on a `<canvas>` and tracks drag rotation in `accumDeg` (can exceed
360° — multiple laps are how you dial in minutes beyond one). On release it
starts a `requestAnimationFrame` countdown computed from real elapsed time
(`performance.now()`), not a decrementing counter, so it stays accurate even
if the tab throttles. The hand angle during countdown is
`(remainingSeconds % 60) / 60 * 360` — it only shows the current minute's
lap, exactly like a mechanical kitchen timer. A fresh touch on the dial while
it's running resets and rewinds it (see `onDown`'s `if (running) resetAll()`).

**Visual redesign (Update 1.2)**: `draw()` no longer strokes an outer circle
around the dial — just the 60 tick marks floating on the page background
(5 of them "major"/longer every 5th tick). The hand is a plain filled
triangle (`#ffffff`, tapering from a `baseHW`-wide base at the center to a
sharp point at the tip — computed from a perpendicular offset at `handA
± 90°`) with a thin `--ink` outline at low alpha for contrast against a
light background, and deliberately **no center dot**. If the hand ever
needs a different look again, `draw()`'s hand-drawing block (search "Update
1.2: hand is now a plain white needle") is self-contained and doesn't touch
the tick-mark loop above it or the countdown math elsewhere in the file.

## Explicit dark/light mode chips (Update 1.3)

Replaced the old single `#theme-toggle-btn` icon (ambiguous — unclear which
mode tapping it would switch *to*) with two explicit chips reusing the
`.goal-chip` style, `THEMES = [{id:"light", label:"Hell"}, {id:"dark",
label:"Dunkel"}]` rendered into `#theme-grid` by `renderThemeGrid()`.
`applyTheme(t)` sets `document.documentElement`'s `data-theme` attribute,
persists to `localStorage.bymgro_theme`, and re-renders the grid so the
active chip highlights. Applied once on boot from the stored value
(defaulting to `"dark"` if never set). Add a new theme by adding one entry
to `THEMES` plus its CSS custom-property block, same pattern as `GOALS`.

## Defensive error handling on data-fetching screens (Update 1.3)

User reported the Fortschritt screen appeared permanently stuck on "Lädt…".
Root cause not reproduced against a copy of their live DB (API + rendering
both worked fine in testing), but the underlying code defect was real and
worth fixing regardless: `renderProgress()`, `bootstrap()`, and
`loadSocial()` all had bare `await api(...)` calls with no error handling —
any single failed/slow fetch (e.g. mid-`--reload` while new files land on
disk) left the loading placeholder stuck forever with no way to recover
short of a full page reload. All three now wrap their fetch in try/catch:
`renderProgress()` swaps in an error state with a "Nochmal versuchen" retry
button that just re-calls itself; `bootstrap()` and `loadSocial()` show a
toast and fail gracefully instead of leaving stale/blank UI. If a *new*
"stuck loading" report comes in after this, it should now at minimum show
an explicit error+retry instead of hanging silently — check server logs for
what the actual fetch failure was, since the symptom is now handled but the
underlying trigger (suspected: transient failure during a server restart)
is still not 100% confirmed.

## Known gaps / next steps (not yet built)

- No auth/multi-user — single local profile, matches "just for me" framing
  of the original ask. Would need the same session_id pattern as memori-mvp
  if this ever needs multiple people's data on one deployed server.
- No persistent Railway volume configured yet — same risk memori-mvp hit
  (SQLite file lives in the container's ephemeral filesystem unless a volume
  is attached and `BYMGRO_DB_PATH` is pointed at it). Set this up before
  relying on Main for real data long-term: Railway dashboard → Service →
  Settings → Volumes → New Volume, mount it, then set `BYMGRO_DB_PATH` env
  var to that mount path.
- Plan editor has no drag-and-drop reorder, just ↑/↓ buttons — fine for a
  short list, revisit only if it feels clunky in practice.
- **Greek-statue exercise illustrations (asked for in Update 1.3, not yet
  built)**: per-exercise two-pose animation, gendered, halftone
  white-on-black marble-statue style art with the worked muscle(s) shown in
  red, displayed on the workout screen while doing that exercise. No
  image-generation tool is available in the Cowork cloud sandbox this
  project is built in, so Claude cannot produce the artwork directly —
  needs a decision from the user on how to source it (they supply/commission
  the artwork and Claude wires it up with gender + muscle-highlight logic,
  vs. a simpler hand-coded SVG substitute Claude builds directly) before
  this can move forward.
