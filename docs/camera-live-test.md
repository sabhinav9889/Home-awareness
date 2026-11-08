# Camera live test (morning checklist)

Home-awareness tool. Not a medical device, security alarm, or emergency service.

Time needed: about 20 minutes. Run every command from the `edge/` folder
(`cd ~/home-awareness/edge`, or wherever the repository is). Use the real server, or your
local one (`http://localhost:8000`). Details of each command: [edge/README.md](../edge/README.md#camera-sensor-ha-camera).

Safety first: **do not fall on purpose**, anywhere in this test.

## 1. Install and fetch the models

    cd edge && uv sync --extra camera
    uv run ha-camera fetch-model

Expect: two file paths printed (the pose and object models). If you see "Cannot download
the camera models: ...", check the internet and run it again.

## 2. Add the camera in the dashboard and pair it

1. Dashboard, **Sensors**, **Add a camera**. Pick the **room**, tick **"Everyone who may be
   on this camera knows about it"** (both are required), press **Create camera pairing
   code**. The code is valid for 10 minutes.
2. Pair:

       uv run ha-camera pair --server http://localhost:8000 --code THECODE

   Expect: `Paired as device N.` and a line saying the camera is watching, frames are
   analysed on this computer, and snapshots go only with alerts.
3. List the cameras:

       uv run ha-camera cameras

   Expect a table with INDEX, NAME and NODE. If there is more than one, remember a unique
   part of the name for `--camera "NAME"` in the commands below. The camera light stays off:
   this command does not open the camera.

## 3. `ha-camera check`: stand in view

Stand where the camera sees you, with a light on, and run:

    uv run ha-camera check

Expect: `Looking through: <camera> for 15 s. Nothing is recorded or sent.`, then lines like
`#12   torso 8 deg    person 0.91  guarding yes would send: ...`, then a Summary and one
line of advice.

What to look for:

- `person` around 0.5 or more while you are in view: the camera sees you.
- `torso ... deg` instead of `no body`: shoulders and hips are visible. A laptop webcam
  often shows only head and shoulders, so `no body` is normal there (it only matters for the
  fall and lying-still tests).
- `guarding no` while Away is off: normal; the person alert needs Away (step 4).
- "The camera sees you well enough." is the good ending. Otherwise:
  - "The picture is too dark..." turn on a light and run it again.
  - "No body was visible for most of the check..." step back so head to hips is in view.
  - "No pictures came from the camera: it is probably busy or unplugged..." close any
    browser tab or app using the camera.
- The summary says "Nothing was sent." Nothing is sent by `check`.

## 4. Away on, then walk into view

1. Dashboard, **Overview**, press **Away** (it shows "Away — guarding"). Wait about **20 s**:
   a running camera re-reads the settings about every 15 s.
2. Leave the camera's view, then start it:

       uv run ha-camera run

   Expect: `Camera: <name>`, then `Watching — video is analysed on this computer; only a
   snapshot goes with an alert.`, then log lines such as `enabled: ...`.
3. Walk into view and stand there a few seconds. Expect a log line
   `detected person_away (0.xx) with snapshot`, then `sent 1 event(s)`, and on the dashboard
   an alert **"Person while guarding"** with the photo (also on Telegram or ntfy if set up).
4. Cooldown: stay in view or walk out and in again. For the next **5 minutes** this camera
   sends no second person alert. That is on purpose.

## 5. Away off: person alerts stop

With `run` still going, press **Away** again so it shows Home. Wait about 20 s, then walk
into view. Expect: the log shows `guarding is now off` and no new `detected person_away`
(also nothing after the 5-minute cooldown). Turn Away back on afterwards if you want to
continue.

## 6. Camera busy test

On Linux the **second** program to open the camera is usually the one that is refused, so
open the camera in a browser tab **first**: any "test webcam" page, allow it, keep it open.

Then start `ha-camera run`. Expect, within a few seconds, **one** warning (not repeated
every retry): `camera busy or unplugged: close any app using it (browser, video call) or
plug it in; trying again every 5 s`. Heartbeats keep going. Now close the browser tab.
Expect `camera is back` within about 5 s and detection to resume.

(If you start `run` first and then open the browser, the browser may be the one that is
refused, and `run` keeps the camera: that is also fine, but it does not test the warning.)

Optional: unplug a USB camera instead. Same warning; plug it in and `camera is back`.

## 7. Stop with Ctrl-C

Press Ctrl-C in the `run` window. Expect it to return to the prompt within a few seconds
with no traceback. Check: `echo $?` prints `0`.

## 8. "Send snapshots" off: no photo

1. Dashboard, **Sensors**, the camera's card: switch **"Send snapshots with alerts"** off
   (owner only). Turn Away on if it is off.
2. Wait about 20 s, start `uv run ha-camera run`, walk into view.
   (If the 5-minute person cooldown from step 4 is still running, wait for it, or restart
   `run`, which resets it.)
3. Expect `detected person_away (0.xx)` **without** "with snapshot", and the dashboard
   alert has no photo. Switch it back on if you like.

## 9. Lying still (easier) and fall (optional, careful)

Both need the camera to see your **shoulders and hips and the floor**, so a laptop webcam on
a desk will not work. Use a wider view (a USB camera on a shelf angled down) and check it
first with `uv run ha-camera check` (you want `torso ... deg` lines, not `no body`).

**Lying still:** put a mattress or yoga mat in view. Run `uv run ha-camera run`, lie on it
still for about **2 minutes**. Expect `detected lying_still (1.00)` and a **"Lying still
(possible collapse)"** alert with a photo. Cooldown 10 minutes.

**Fall: do NOT fall on purpose.** Skip this unless you can do it safely: use a thick
mattress, with an adult spotter next to you, and only simulate a slow collapse onto the
mattress. A real fall is quick (upright to flat in about 1 s), so a slow simulation may not
trigger it, and that is fine. If in any doubt, skip it. If it triggers you will see
`detected fall (1.00)` and a **"Fall"** alert (cooldown 2 minutes).

## What to tell me

Paste these back:

1. The whole output of step 3 (`ha-camera check`), at least the last 8 lines, including
   Summary and the advice line.
2. The log lines from `ha-camera run`: everything from `Camera:` to the end, for steps 4 to 8
   (the `enabled:`, `guarding is now`, `detected`, `sent`, `camera busy`, `camera is back`
   lines).
3. What the dashboard showed: was there an alert, did it have a photo, how long after
   walking into view?
4. Your camera's name (from `ha-camera cameras`) and whether it is a laptop webcam or USB.
5. Anything odd: wrong alerts (a TV, a poster), no alerts, error messages (copy them exactly).

Do not paste the contents of `camera.json` (it holds the device key).
