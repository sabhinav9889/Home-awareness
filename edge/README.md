# home-awareness-edge

Home-awareness tool. Not a medical device, security alarm, or emergency service.

Edge client for Home Awareness: `ha-sensor` turns a laptop into a sound sensor, `ha-camera`
turns a camera into a sensor ([see below](#camera-sensor-ha-camera)), `ha-agent` shows alerts
and plays an alarm. Run every command below from this `edge/` directory.

## Requirements

- Linux with PipeWire and its command-line tools `pw-record`, `pw-dump` and `pw-play`
  (Ubuntu/Fedora desktops ship them; on Debian/Ubuntu they are in `pipewire-bin`).
- Python 3.12 and [uv](https://docs.astral.sh/uv/).
- A Home Awareness server you can log in to (see the [root README](../README.md)).

Install:

    cd edge && uv sync

(`uv sync --dev` also installs the test tools: `uv run pytest -q`.)

Config lives in the platformdirs user config dir (`sensor.json`, `agent.json`, mode 0600, in
a directory created with mode 0700); set `HA_EDGE_CONFIG_DIR` to override.

## Sound sensor (`ha-sensor`) quick start

1. Download and verify the YAMNet sound model (once, ~4 MB, from Google's public URL with
   a pinned SHA-256):

       uv run ha-sensor fetch-model

2. Create a pairing code on the server. Until the dashboard lands, use the API: log in,
   then `POST /devices/pairing-codes` with `{"name": "Kitchen laptop", "kind": "laptop"}`
   (in the server's `/docs` page, Authorize with your login token first). The 8-character
   code is valid for 10 minutes and works once.
3. Pair this laptop (the device key is stored in `sensor.json` and never printed again):

       uv run ha-sensor pair --server https://example.org --code ABCD1234

4. See which microphones there are (`*` marks the system default input):

       uv run ha-sensor devices

5. Start listening (Ctrl-C or SIGTERM stops it):

       uv run ha-sensor run                  # the default input, or the saved one
       uv run ha-sensor run --mic "ALC257"   # a name, or a unique part of it

What to know:

- **The microphone is pinned.** The first `run` (or `run --mic NAME`) saves the exact
  device name; later runs use exactly that device. If it disappears (unplugged, or a
  headset taking over), the sensor **never switches to another input**: it logs the
  problem and exits with code 4. Pick a new one with `run --mic NAME`.
- **Nothing is detected until the first heartbeat succeeds.** The server's heartbeat says
  which sounds this household enabled; until it answers, every detection is dropped (it
  retries every 5 s). Only enabled sounds are ever sent, and only the sound type, a
  confidence and a time: raw audio never leaves the process and is never written to disk.
- **Each sound has its own detection threshold**, sent by the server with the heartbeat:
  the lowest any household member set for that sound (defaults from measured accuracy:
  0.50 for siren, coughing, dog barking and crying, 0.30 for the rest). A server too old to
  send thresholds means 0.30 for every sound.
- **A detection is reported about 0.5 s after it is first heard.** The sensor waits one
  more listening window and reports the louder of the two, so a sound's quiet onset
  doesn't hide its peak from members with a stricter threshold. A detection still
  waiting when the sensor stops is sent before it exits (best effort, up to 3 s).
- **Audio playing through the speakers raises the detection threshold** (by 0.20 per
  sound, e.g. 0.30 to 0.50, capped at 0.95), so a film is less likely to trigger alerts.
  This also makes real sounds slightly harder to catch while something plays, and it
  isn't perfect: loud media can still trigger. Playback through **headphones** doesn't
  count (the mic can't hear it). Only the **default output** is considered: audio sent
  to another output device is treated as not playing.
- Events the server rejects are dropped with a logged reason (at most once per reason per
  10 minutes). If the laptop's clock is more than 10 minutes off, every event is rejected:
  fix the system time. A wrong `--server` shows up as an HTTP 404.

## Check your microphone

Before relying on the sensor, check what it hears (it can run while `ha-sensor run` is
running; nothing is recorded or sent):

    uv run ha-sensor check                 # 15 s on the pinned (or default) microphone
    uv run ha-sensor check --seconds 30 --mic "USB"   # another input; the pin is not changed

Make the sounds you care about (ring the doorbell, clap, shout) while it listens. About
twice a second it prints the input **level** (RMS) and **peak**, a **CLIPPING** marker
when at least 1 % of samples hit full scale, YAMNet's top three classes, and the catalog
sounds scoring 0.05 or more: `✓` means the sensor would report it under your household's
current thresholds (fetched from the server when paired, else 0.30), `(off)` means nobody
turned that sound on. A summary follows: loudest level and peak, how many windows clipped,
the best score per sound, and one line of advice.

- **Clipping** (peaks at 1.00): the input level is too high, and loud sounds get
  distorted beyond recognition (a shouted scream was heard as snoring and coughing).
  Lower it, e.g. `wpctl set-volume @DEFAULT_AUDIO_SOURCE@ 0.3`, and check again. While
  running, the sensor also warns in its log (at most every 5 minutes) and the dashboard
  shows "Microphone is overloading" on the sensor's card.
- **Too quiet** (the loudest peak stays under 0.10): raise it, e.g.
  `wpctl set-volume @DEFAULT_AUDIO_SOURCE@ 0.7`, or move the laptop closer.

`ha-sensor` exit codes: 0 stopped normally, 1 network or model error (or the microphones
can't be listed), 2 usage error, unusable config, pairing refused or rate limited, 3 not
paired, 4 microphone not found or lost, 5 this sensor was removed from the household (pair
again).

Accuracy measurements per sound: see [docs/accuracy.md](../docs/accuracy.md) (harness: `scripts/eval_accuracy.py`).

## Camera sensor (`ha-camera`)

`ha-camera` watches a laptop webcam or USB camera on Linux and reports a short event when
it sees something worth an alert. The pictures are analysed on this computer by two small
models (MediaPipe pose and object detection); frames are never saved or sent. The only
picture that ever leaves is **one still photo with an alert**, and only if the camera's
"Send snapshots" switch is on. Step-by-step first test: [docs/camera-live-test.md](../docs/camera-live-test.md).

> Not a medical device, security alarm, or emergency service. Accuracy has **not** been
> measured on real people yet: expect misses and false alarms.

No camera handy? `scripts/fake_camera.py` (from the repository root) pairs with a camera
pairing code and sends a fall, lying-still, person or weapon event with a JPEG, so you can
try camera alerts, photos and incidents without a camera (see the script's header).

### Quick start

1. Install with the camera extra (adds OpenCV and MediaPipe):

       cd edge && uv sync --extra camera

2. Download and verify the two models (once; pinned SHA-256):

       uv run ha-camera fetch-model

   It prints the two file paths. If it prints "Cannot download the camera models: ...",
   check your internet connection and run it again; a file that fails its checksum is
   refused.
3. In the dashboard open **Sensors → Add a camera**, pick the **room**, and tick
   **"Everyone who may be on this camera knows about it"** (both are required), then press
   "Create camera pairing code". The code works once and is valid for 10 minutes.
4. Pair this computer (the device key is stored in `camera.json`, mode 0600, and is never
   printed):

       uv run ha-camera pair --server https://example.org --code ABCD1234
       uv run ha-camera pair --server https://example.org --code ABCD1234 --camera "Integrated"

   `--camera` is a camera name or a unique part of it; it is saved for later.
5. See which cameras there are (this lists them without opening them, so it never turns a
   camera light on):

       uv run ha-camera cameras

6. Look at what the camera sees, with nothing sent (next section), then start it:

       uv run ha-camera run                  # the saved camera, or the only one
       uv run ha-camera run --camera "USB"   # a name, or a unique part; saved for next time

   Ctrl-C (or SIGTERM) stops it cleanly with exit code 0. If there is more than one camera
   and none is saved, it asks you to choose with `--camera NAME`.

### `ha-camera check`: what does it see?

    uv run ha-camera check                    # 15 s on the saved (or only) camera
    uv run ha-camera check --seconds 30 --camera "USB"   # another camera; not saved

It uses the household's real settings from the server when paired (or says it is using
defaults if the server cannot be reached), looks for `--seconds`, and prints one line about
four times a second, for example:

    #12   torso 8 deg    person 0.91  guarding yes would send: person_away

- `torso 8 deg` / `no body`: the angle of your torso from vertical (0 upright, 90 lying
  flat), or `no body` when the shoulders **and** hips were not both clearly visible.
- `person 0.91`: how sure the object model is that a person is in view (`person -` means
  it was not run).
- `guarding yes/no`: whether the household is Away or in night hours (see below).
- `would send: ...`: the events that frame would raise. `check` never sends anything.

It ends with a summary (frames, how many had a body and a person, average brightness out of
255, what would have been sent) and one line of advice:

- "The picture is too dark..." (average brightness under 40): turn on a light.
- "No body was visible for most of the check..." (a body in fewer than 1 frame in 5): move
  back so the camera sees your head to your hips.
- "No pictures came from the camera: it is probably busy or unplugged..." : close the app
  or browser tab using it.
- "The camera sees you well enough."

### What `ha-camera run` does

It opens the camera, analyses about 4 frames a second, and sends events with the device
key. On the first start it prints the camera name and a notice that it is watching. It
logs what it does ("enabled: ...", "detected person_away (0.91) with snapshot", "sent 1
event(s)").

- **Nothing is detected until the first heartbeat succeeds** (log: "waiting for the server;
  detection starts when it answers"); it retries every 5 s.
- **It re-reads the household's camera settings from the server about every 15 s**
  (enabled events, thresholds, Away mode, the "Send snapshots" switch) and on every
  heartbeat, so a change in the dashboard reaches a running camera within about 15 s.
  Models for events nobody enabled are not run.
- **Cooldowns on the device** so one episode does not flood the household: a fall 2 min,
  lying still 10 min, person while guarding 5 min, weapon seen 5 min. The server applies
  its own rules, cooldowns and incidents on top.
- If the server is unreachable, events wait in a small queue and go out when it answers.
- If the camera is removed in the dashboard, `run` logs "This camera was removed. Pair it
  again with `ha-camera pair --server URL --code CODE`." and exits with code 5.

### Away mode ("Person while guarding")

"Person while guarding" (`person_away`) fires only while the household is **guarding**:
Away mode is on (the **Away** button on the dashboard Overview, any member can press it) or
the time is inside the household's night hours (set by the owner under Household →
Cameras). Person detection is not even run while the house is not guarding. After you
switch Away on, the camera notices within about 15 s (its next settings refresh; sometimes
the heartbeat brings it sooner), so wait about 20 s before walking into view. Falls and
lying-still do not need Away: they are always on when enabled.

### Photos

When an alert is raised and the camera's **"Send snapshots with alerts"** switch (Sensors
page; owner-only) is on, the alert carries one JPEG: at most 640 px on the longest side and
300 KB, made in memory from the frame that triggered the alert, never written to this
computer's disk. A frame more than 1 s old is not sent. The server stores it encrypted,
shows it only to household members, and deletes it after the household's retention setting
(1, 7 or 30 days; default 7; set by the owner under Household → Cameras). With the switch
off, camera alerts carry no photo (a queued photo is dropped when the switch is turned off).
Cameras in a bedroom or bathroom start with photos off.

### What each detection needs

| Event (dashboard name) | Needs | Default |
|---|---|---|
| Fall (`fall`) | Whole upper body (both shoulders and both hips) in view, **and room around it to see the torso go from upright to horizontal within about 1 s**; decent light | on |
| Lying still (`lying_still`) | Shoulders and hips in view while lying (so the floor must be in view), torso flat and almost motionless for about 2 minutes | on |
| Person while guarding (`person_away`) | A person visible for 3 of the last 5 analysed frames, light, and the house guarding | on |
| Weapon seen (`weapon_seen`) | A "knife" or "baseball bat" label from the object model; **experimental** | **off** |

- **Light matters.** Most webcams are poor in the dark; with a dark picture nothing works.
- **A laptop webcam usually sees only your head and shoulders.** Falls and lying-still
  need the shoulders **and** hips, so a laptop on a desk will mostly say "no body". Use a
  camera with a wider view that includes the floor (a USB camera on a shelf or a wall,
  angled down).
- **When the body is not visible the program stays silent by design** (fall and
  lying-still never fire on a guess). Run `ha-camera check` to see what your camera sees.

### Honest limits

- **"Suspicious" here means exactly four things:** a person while the house is guarding, a
  fall, lying still for about 2 minutes, and a weapon label (off by default, experimental).
  **Stealing and smashing cannot be detected from video.** They only show up through the
  server's combined incidents, for example "Possible break-in" (a breaking-glass sound plus
  a person or a camera person event) or "Property damage".
- **False alarms:** a TV, screen or poster showing people can count as a person; kitchen
  knives (and bats in a game) can trigger weapon seen, which is why it is off; sitting down
  fast or kids playing can look like a fall. Pets never trigger camera events.
- **The object detector never reports below 0.4**, even if your household threshold for
  person or weapon is lower.
- **Weapon seen fires on any knife or bat in view**, not only ones being held.
- **Someone lying along the camera's line of sight looks upright** to the pose model, so a
  fall or lying-still in that direction is missed. The "low in the frame" idea from the
  design is not implemented.
- **Pair with a camera pairing code.** A code made for a sound sensor pairs fine, but that
  household setting keeps no photos.
- **No face recognition and no identity**, ever.
- **Accuracy has not been measured on real people yet.** Treat it as a helper, not a guard.

### Troubleshooting

- **"camera busy or unplugged: close any app using it (browser, video call) or plug it
  in; trying again every 5 s"**: another program (a browser tab, a video call) has the
  camera, or it was unplugged. Close the other program or plug it in; `run` reconnects by
  itself and logs "camera is back". It logs the warning once, not every retry.
- **"Camera 'X' not found" / "matches more than one camera"**: run `ha-camera cameras` and
  pass a more exact `--camera NAME`.
- **"This camera was removed. Pair it again..."**: someone removed this camera in the
  dashboard. Create a new camera pairing code and run `ha-camera pair` again.
- **"The camera models are missing. Run `ha-camera fetch-model` first."** or **"Cannot
  download the camera models: ..."**: connect to the internet and run `fetch-model`.
- **"The camera extra is not installed. Run `uv sync --extra camera`."**
- **"Not paired. Run `ha-camera pair --server URL --code CODE` first."**
- **"Pairing failed: ... Create a new pairing code and try again."**: the code expired or
  was used; "answered 404 Not Found" means the `--server` address is wrong.

`ha-camera` exit codes: 0 stopped normally, 1 network, model or camera error, 2 usage error,
unusable config, pairing refused or rate limited, 3 not paired, 5 this camera was removed.

## Alert agent (`ha-agent`)

For the laptop you sit at, headphones on: alerts for you pop up on the desktop and, if you
enabled "alarm" in your alert preferences, an 880/660 Hz alarm plays through the **default
output** — the headphones while they are in use — for up to 30 s per alert (3 minutes at
most in a row).

    uv run ha-agent login --server https://example.org --email you@example.org   # asks for the password
    uv run ha-agent run          # Ctrl-C (or SIGTERM) stops any alarm and exits 0
    uv run ha-agent test-alarm   # plays the alarm for 3 s

- `login` signs in with your password once, then creates a long-lived **agent token**
  (named "`<hostname>` agent") and stores only that token and your member id in
  `agent.json` (mode 0600). The password and the short-lived login are never stored, and
  no token is ever printed or logged. You do not need to log in again every day.
- Logging in again on the same laptop saves the new token first and then revokes the old
  one on the server. If the new token cannot be saved (disk full, permissions), `login`
  revokes the new token, keeps the old login working and exits 1.
- The agent token is narrow: it only opens the live alert connection and sends "I'm on it".
  Every other API call refuses it. Each account can have up to 10 active agent tokens.
- **Revoke** a laptop's token (lost laptop, uninstall) until the dashboard has a button:
  `GET /me/agent-tokens` lists them (id, name, last used), and
  `DELETE /me/agent-tokens/{id}` revokes one, both with a normal login's bearer token. A
  revoked token's open connection keeps running until it next reconnects; that reconnect
  is refused and `run` exits 6.
- Configs saved by older versions (a 24 h login) still work until they expire; `run` then
  prints "re-run `ha-agent login` to stop daily logins".
- Popup buttons: **I'm on it** (acknowledges for the household and stops the alarm) and,
  on alarm alerts, **Stop alarm** (either one silences the alarm). Popup-only alerts show
  just **I'm on it**. When someone acknowledges an alert you
  received, you see who, and the alarm stops once every alert it is sounding for has been
  acknowledged. Acks for other members' alerts (including their test alerts) are ignored.
  Each alarm alert rings for up to 30 s; a new alarm alert while one sounds restarts the
  30 s window, but continuous ringing is capped at 3 minutes from when it first started.
  After that, new alerts show the popup only until the alarm has been quiet; then the next
  alarm alert rings again. Forgetting an old alert (the agent remembers the last 200) also
  silences an alarm that was sounding only for it.
  The popup is shown even when only the alarm is enabled — it is the stop button.
- Without a session D-Bus/notification server, alerts are printed in the terminal instead;
  without button support, the alarm stops after its 30 s (3 minutes at most in a row, see
  above) or on Ctrl-C.
- The alarm needs PipeWire's `pw-play`. If the default output is muted, you won't hear it.
- When the token is revoked (or an old 24 h login expires) or you are removed from the
  household, `run` shows "Please log in again: ha-agent login" and exits 6. Other
  disconnects reconnect with backoff 1, 2, 4 … 60 s (±20 % jitter).

Exit codes: 0 ok, 1 network/audio error, 2 usage or login rejected, 3 not logged in, 6 log in again.
