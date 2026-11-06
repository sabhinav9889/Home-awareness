# home-awareness-edge

Home-awareness tool. Not a medical device, security alarm, or emergency service.

Edge client for Home Awareness: `ha-sensor` turns a laptop into a sound sensor, `ha-agent`
shows alerts and plays an alarm. Run every command below from this `edge/` directory.

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

`ha-sensor` exit codes: 0 stopped normally, 1 network or model error (or the microphones
can't be listed), 2 usage error, unusable config, pairing refused or rate limited, 3 not
paired, 4 microphone not found or lost, 5 this sensor was removed from the household (pair
again).

Accuracy measurements per sound: see [docs/accuracy.md](../docs/accuracy.md) (harness: `scripts/eval_accuracy.py`).

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
