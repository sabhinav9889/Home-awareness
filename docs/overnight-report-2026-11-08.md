# Overnight report: 8 Nov 2026 (01:12 → 05:05 IST)

Nothing was pushed or merged. Everything below is local, waiting for your review.

## ⚠️ First, one thing that needs you: the live database

During the final fix round, a builder agent's test command broke halfway. Its database-upgrade step then ran against your **live `home` database** instead of a throwaway copy, because the camera worktree's `.env` pointed at the live DB.

- **Effect:** the live DB is now at migration **0017** (camera branch). `main` only knows up to **0014**.
- **Data:**
  - The new tables (rooms, snapshots) are empty.
  - The new columns all have safe defaults: `away=false`, retention 7, `capabilities='audio'`, `send_snapshots=true`.
  - No existing data was changed or lost.
- **Your running app:** I checked it read-only at 04:57. `/health` returned 200 and `/app/` returned 200. The old code ignores the extra columns.
- **Not undone:** the agent's attempt to undo it was blocked by your permission settings. I didn't run it either, since it's your call.
- **Your options:**
  - **Leave it.** Everything keeps working, and you'll need these migrations anyway if you merge the camera branch.
  - **Undo it:**

    ```
    cd ~/home-awareness-camera
    DATABASE_URL=postgresql+asyncpg://home:<password>@localhost:5432/home uv run alembic downgrade 0014
    ```
- **Guard added:** the camera worktree's `.env` now points at `home_camera_test`, so this can't recur from there.

## 1. CI fix: branch `ci-fix` (main checkout), ready to push

The CI failure was one phone-width test: GitHub's runner fonts are wider, which pushed the Result chip off screen.

| Commit | What it does |
|---|---|
| `4ef4ba9` | Fixes the table layout at phone width. The test now also runs with a deliberately wide font. |
| `cf30632` | Refreshes `docs/screenshots/overview-mobile.png`. The Result chip is visible. |

- **Result:** 400 tests passed locally.
- **To publish:** review it, then merge into main and push. Long sound names now wrap mid-word in that phone table ("Glass breakin g"). It's readable, but you may want a nicer wrap later.

## 2. Camera feature: Plan 5a (server + dashboard), branch `camera-design`

Built in the worktree `~/home-awareness-camera` with subagent-driven development: one implementer per task, an independent reviewer per task, fix rounds, then a whole-branch review and a final fix wave that was itself reviewed.

| Commit | What |
|---|---|
| `fd06504` | Rooms, camera devices (`kind=camera`, capabilities audio/video), consent. Owner-only room management. Bedroom/bathroom cameras default to photos off. |
| `db03dc4` | Camera event types (fall, lying still, person while guarding, weapon seen — off, experimental) and incident types. Away mode (any member), night hours and photo retention (owner). `guarding` is sent to sensors. |
| `8183002` + `4834891` | Alert photos: one JPEG ≤ 300 KB, Fernet-encrypted on disk, members only, deleted after 1/7/30 days, 200 MB per household cap. Telegram `sendPhoto` and ntfy attachments, each with a safe text fallback. |
| `b108e47` + `7309123` | Combined incidents per room: Possible break-in, Fall confirmed, Possible property damage. They're follow-up alerts and never delay the normal alerts. |
| `3a02fae` | Dashboard: Rooms page, "Add a camera" (room plus consent required), Away button, night hours and retention settings, and alert photos (fetched with your login as a blob — no token in any URL). |
| `59d53f9` | Final review fixes (15 items, listed below). |

**Tests:**
- Server: **538 passed** (includes real-Chrome Playwright tests).
- Edge client: 311 passed.
- ruff: clean.
- Migration chain 0014 → 0017 → 0014 → 0017 plus `alembic check`: clean on a populated throwaway DB.

### Calls I made for you (please check)

| # | Ruling |
|---|---|
| 5a-R1 | The camera branch uses its own test DB, `home_camera_test`. Its schema differs from main, and sharing `home_test` broke main's tests. |
| 5a-R2 | Uploads up to 8 MB are accepted. A photo that is too big is dropped; the alert is kept. Anything over 8 MB is refused. |
| 5a-R3 | Photo uploads that don't declare their size are refused (411), so they can't fill the disk. |
| 5a-R4 | If one set of events matches both "break-in" and "property damage", only "Possible break-in" is sent. |
| 5a-R5 | An incident alert gets its own encrypted copy of the camera photo, so retention and deletion stay simple. |
| 5a-R6 | Incident checking runs after the normal alerts are saved and sent. It can never fail or delay them. |
| 5a-R7 | Incidents skip the 5-second cross-sensor merge, so two rooms each get their own alert. Per-member cooldowns still apply. |
| 5a-R8 | "X turned Away on" shows on every dashboard page. |
| **5a-R9** | **The server only alerts "person while guarding" when the house really is guarding.** Cameras learn about Away from their once-a-minute settings update, so without this you'd get alerts with photos of your own family for up to a minute after turning Away off. Trade-off: after Away is switched on, the camera may report nothing for up to a minute. |
| **5a-R10** | **Moving a camera into a bedroom or bathroom, or changing a room to one, switches its photos off.** The owner can turn them back on. |

**Final-review fixes (`59d53f9`):**
- The camera now receives the "Send snapshots" switch, so with it off no photo leaves the device.
- R9 and R10 above.
- A rollback now cleans up camera events.
- Night hours with a timezone offset are refused.
- Telegram only falls back to text when it clearly refuses the photo.
- No photo files are left over when an alert fails.
- The room dropdown no longer resets while you use it.
- Photo retry works.
- Past events show their photo.
- Cameras are labelled "Camera".
- The disclaimer now says "Cameras send a single snapshot only when they raise an alert."

**Smaller follow-ups** are in `.superpowers/backlog-plan5a-minors.md`. Examples:
- Away changes aren't written to history.
- A deleted household's photo files linger up to a day before cleanup.
- Consent given at pairing doesn't record who confirmed it.

## 3. Not done yet

- **Plan 5b:** the `ha-camera` client (laptop/USB/Pi webcam with on-device pose and object models). This is what actually detects falls and people.
- **Plan 5c:** local HTTPS (Caddy) plus Android sensor mode (phone mic and camera in Chrome).

I didn't start these overnight. They involve downloading models and touching your camera hardware, which is better done with you awake.

## 4. Waiting on you

1. The live DB decision (top of this report).
2. Push `ci-fix`?
3. Review the camera branch and rulings R9/R10. Then: merge `camera-design` into main?
4. Still open from before:
   - mark scream "experimental" and measure it;
   - Plan 4 (cloud deployment);
   - write Plans 5b/5c.
