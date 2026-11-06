# home-awareness-edge

Home-awareness tool. Not a medical device, security alarm, or emergency service.

Edge client for Home Awareness: `ha-sensor` turns a laptop into a sound sensor, `ha-agent`
shows alerts and plays an alarm.

    uv sync --dev
    uv sync --extra portaudio   # live microphone capture; needs libportaudio2

Config lives in the platformdirs user config dir (`sensor.json`, `agent.json`, mode 0600);
set `HA_EDGE_CONFIG_DIR` to override.

Accuracy measurements per sound: see [docs/accuracy.md](../docs/accuracy.md) (harness: `scripts/eval_accuracy.py`).

## Alert agent (`ha-agent`)

For the laptop you sit at, headphones on: alerts for you pop up on the desktop and, if you
enabled "alarm" in your alert preferences, an 880/660 Hz alarm plays through the **default
output** — the headphones while they are in use — for at most 30 s.

    ha-agent login --server https://example.org --email you@example.org   # asks for the password
    ha-agent run          # Ctrl-C (or SIGTERM) stops any alarm and exits 0
    ha-agent test-alarm   # plays the alarm for 3 s

- `login` stores only the access token (valid 24 h) in `agent.json`; the password is never
  stored, printed or logged, and neither is the token.
- Popup buttons: **I'm on it** (acknowledges for the household and stops the alarm) and
  **Stop alarm**. When someone else acknowledges, the alarm stops and you see who.
  The popup is shown even when only the alarm is enabled — it is the stop button.
- Without a session D-Bus/notification server, alerts are printed in the terminal instead;
  without button support, the alarm stops after 30 s or on Ctrl-C.
- The alarm needs PipeWire's `pw-play`. If the default output is muted, you won't hear it.
- When the token expires or you are removed from the household, `run` shows
  "Please log in again: ha-agent login" and exits 6. Other disconnects reconnect with
  backoff 1, 2, 4 … 60 s (±20 % jitter).

Exit codes: 0 ok, 1 network/audio error, 2 usage or login rejected, 3 not logged in, 6 log in again.
