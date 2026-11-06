# home-awareness-edge

Home-awareness tool. Not a medical device, security alarm, or emergency service.

Edge client for Home Awareness: `ha-sensor` turns a laptop into a sound sensor, `ha-agent`
shows alerts and plays an alarm.

    uv sync --dev
    uv sync --extra portaudio   # live microphone capture; needs libportaudio2

Config lives in the platformdirs user config dir (`sensor.json`, `agent.json`, mode 0600);
set `HA_EDGE_CONFIG_DIR` to override.

Accuracy measurements per sound: see [docs/accuracy.md](../docs/accuracy.md) (harness: `scripts/eval_accuracy.py`).
