# Home Awareness: competitor and market research (draft)

Date: 2026-11-07. Method: web search summaries only; most pages were NOT opened in full. Prices are from secondary sources and often 2020-2025, so treat them as indicative. Anything marked (unverified) needs a check against the vendor page before you quote it.

## 1. Comparison table

| Product | Detects | Runs where | Audio leaves home? | Alert channels | Multi-member | Cost |
|---|---|---|---|---|---|---|
| Apple Sound Recognition (iOS/watchOS) | Baby cry, smoke alarm, dog, cat, doorbell, knock, running water, cough, shouting [1][2] | On-device [1][2] | No (Apple says on-device) [1] | Notification on the same device/watch only | No (one person, one device) | Free, with an Apple device |
| Android Sound Notifications / Live Transcribe | About 10 sounds at launch, now 30+ events: baby, dog, doorbell, knock, water, appliance beep [3][4] | On-device, works offline [3] | No for sound alerts (speech transcription separate) | Vibrate, camera flash, on-screen notification, timeline [3] | No | Free |
| Google Nest Aware (Nest cams, speakers) | Smoke/CO alarm, glass break; some tiers add emergency calling (US) [5] | Cloud-tied (needs subscription); processing location unverified | Clips can be reviewed in the app, so audio/video reaches Google [5] | Phone push; 911 call in US [5] | Home members via Google Home | Reported about $8/mo or $80/yr (Aware), $15/$150 (Plus) (unverified, secondary) [5] |
| Amazon Alexa Guard / Guard Plus | Glass break, smoke/CO alarms; Plus adds "activity sounds" (footsteps, talking) [6] | Echo device plus Amazon cloud (exact split unverified) | Plus feature records/streams activity sound; detail unverified | Alexa app push, optional emergency help [6] | Household accounts | Plus launched at $4.99/mo or $50/yr; later reported $5.99/mo under Emergency Assist (unverified) [6][7] |
| Ring Alarm Glass Break Sensor | Glass break (acoustic) [6] | On the sensor | No audio sent (event only) | Ring app, siren, optional paid monitoring | Shared Ring account | $39.99 hardware [6]; monitoring extra |
| Wyze cams (Cam v3 / Pan) | Smoke/CO alarm sound; crying, barking, meowing, talking with paid AI [8] | Camera plus Wyze cloud (split unverified) | Video/event clips go to cloud if enabled | App push | Shared account | Camera about $36-40; Cam Plus about $3.99/mo or $29.99/yr in one regional page [8] |
| SimpliSafe / ADT glass-break sensors | Glass break only [9][10] | On the sensor | No | Alarm panel, siren, monitoring center | Per system | Hardware plus monitoring plan (prices not verified) |
| Baby monitors: Nanit, Owlet, Cubo AI | Cry, motion, sleep; Owlet also vitals (Dream Sock is FDA de novo cleared) [11][12] | Camera plus vendor cloud | Video/audio streams to vendor cloud (Owlet app lists data linked to identity) [12] | App push to parents | Multiple caregivers | Camera $300+; subscriptions for core features (Nanit free access 6 months; Cubo AI paid) [11] |
| Apple Watch fall detection | Hard falls | On the watch | No audio | Watch alarm, auto call to emergency services, texts emergency contacts [13] | One wearer, contacts notified | Watch cost only |
| Vayyar Care / Walabot Home | Falls via radar, no camera [14] | Sensor plus cloud (split unverified) | No audio or video | Alexa Together, emergency contacts [14] | Care circle | Walabot $249 launch; Alexa Together fee unverified [14] |
| Bellman & Symfon Visit, Sonic Alert | Doorbell, phone, baby cry, smoke/CO via transmitters [15][16] | Local radio transmitters | No | Flash, vibrating pager, bed shaker, loud chime in up to 4 rooms [15][16] | Several receivers | Hardware only, no subscription (prices not verified) |
| Home Assistant + Frigate audio | Frigate: 500+ AudioSet labels; default bark, fire_alarm, speech, yell [17] | Local CPU, volume-gated [17] | No | Any HA notifier (Telegram, ntfy, mobile app) | HA users | Free software plus a server you run |
| AudioClassifier-MQTT (YAMNet to HA) | 521 YAMNet classes [18] | Local | No | HA automations | HA users | Free; about 19 GitHub stars, hobby grade [18] |
| Ava Pro (Android app for HA) | Smoke/CO, glass break, siren, speech; docs say use certified detectors for critical safety [19] | On-device | No | HA | HA users | Free (unverified) |
| Seeed Sound Event Detection Module D1 | Baby cry, glass break, gunshot, alarm tones, snore [19] | On-board | No | ESPHome/HA | HA users | Hardware; stock status unclear [19] |

Not found: Scrypted audio analytics (search returned nothing, unverified).

## 2. What users value

- **Alerts that reach you when you cannot hear.** Apple and Android built these for deaf and hard-of-hearing users and describe the point as being told about sounds you cannot clearly hear; one reviewer says the benefit outweighs spurious alerts [1][3][20].
- **On-device processing and no subscription.** Apple/Android sound alerts are free and local [1][3]; the open-source stack is popular for the same reason [17][18].
- **Multi-modal alerts** (flash, vibration, bed shaker, pager) in the deaf alerting market [3][15][16].
- **A timeline of what was heard** (Android) [3].
- **Tunable sensitivity and chosen labels.** Frigate's per-label config is the model [17]; Ava Pro offers a sensitivity setting [19].
- **Acknowledgement and shared awareness** (Apple Watch "I'm OK" prompt, care circles) [13][14]. No competitor found does household-wide "I'm on it" for sound events (my reading, unverified).

## 3. What users complain about

- **Subscription lock-in.** Cry and sound detection on Nest, Wyze and Cubo sit behind paid plans [5][8][11]. A Cubo AI owner says the product became "a very basic baby monitor" without paying [11]. Alexa Guard features that were free moved to a paid tier [7]. Nanit: Trustpilot score reported at 1.5 of 5 (one regional page, about 117 reviews), with fees and connectivity cited [11].
- **False positives.** Six Colors calls Apple's feature high in false positives; Cult of Mac says "far from perfect" [20]. Wyze users report smoke-alarm false positives [8]. Glass-break sensors are known for false alarms near TV speakers and kitchens [9][10].
- **Reliability gaps.** An Apple Community thread reports Sound Recognition stopping suddenly [20]. Apple warns against relying on it in high-risk or emergency situations [1].
- **Privacy and cloud.** Owlet's app listing discloses identity-linked data [12]; Nest/Wyze/baby cams send clips to vendor clouds [5][8][11] (general category, not one incident verified).
- **Single-device, single-person limits.** Apple/Android alerts go only to the phone that heard it, so a person elsewhere or in headphones on another device misses it (my inference from the product descriptions [1][3]).
- **Regulatory edge:** Owlet had to stop selling its Smart Sock after an FDA letter in 2021 [12]. Medical claims are costly; our disclaimer avoids this.

## 4. Gaps Home Awareness can credibly fill

1. **Household-level, not device-level.** Sounds heard by a room sensor reach every chosen member on their chosen channel. Apple/Android do not do this [1][3].
2. **Per-person rules**: repeat counts ("5 barks in 60 s") and cooldowns. Frigate has labels but I found no repeat/cooldown model in HA audio docs (unverified) [17].
3. **Alarm through current headphones.** This is the origin story. I found no competitor that does this (unverified claim; absence of evidence).
4. **No subscription, no audio upload, self-hostable**, versus the Nest/Alexa/Wyze/baby-monitor paywalls above.
5. **Sensor health**: loud notice when a sensor goes silent. Competitors do not highlight it (unverified).
6. **Hardware you already own** (spare phone, laptop), versus $40-$300 devices.
7. **Honesty labels**: per-sound reliability measured on ESC-50/AudioSet, versus vendor marketing with no numbers [9][10].

Where we should not compete: certified smoke detection, monitored security, fall rescue, infant vitals. Competitors in those spaces carry certification or regulation, and even open-source Ava Pro says to use certified detectors [19].

## 5. Ranked feature list for Home Awareness

Rank = fit first, then value divided by cost. Cost is for a solo backend dev. Items already in the design are marked (in design).

| # | Feature | What / who has it | Value | Cost | Fit |
|---|---|---|---|---|---|
| 1 | Per-user enabled sounds with measured reliability labels (in design) | Frigate per-label config [17]; Apple/Android fixed lists [1][3] | High | S | fits |
| 2 | Repeat rules plus cooldowns (in design) | Alert-flood control; no competitor found with user-defined N-in-T | High | S | fits |
| 3 | Sensor offline detection via heartbeat (in design) | Avoids silent failure; unverified in competitors | High | S | fits |
| 4 | Alert through current headphones (laptop agent) (in design) | No competitor found (unverified) | High | M | fits with caveats (OS audio routing, laptop must be on) |
| 5 | Telegram and ntfy push, dashboard popup (in design) | HA notifiers; Nest/Wyze app push [17] | High | S | fits |
| 6 | Event timeline / history | Android timeline [3]; Nest history [5] | Med | S | fits |
| 7 | Test alert and "I'm on it" acknowledgement (in design) | Apple "I'm OK" [13]; care circles [14] | Med | S-M | fits |
| 8 | Multi-sensor merge into one incident (in design) | Not found in consumer products | Med | M | fits |
| 9 | Smoke/CO alarm detection | Apple, Android, Nest, Alexa, Wyze, Ava Pro [1][3][5][6][8][19] | High | S (YAMNet class) | fits with caveats (label "good, not a smoke detector", users must keep a real one) |
| 10 | Baby/adult crying | Apple, Android, Nanit, Cubo, Bellman [1][3][11][15] | High | S | fits with caveats (not a baby monitor) |
| 11 | Glass break, doorbell, dog repeat-bark | Ring, SimpliSafe, ADT, Alexa [6][9][10] | Med | S | fits with caveats (false alarms near speakers) |
| 12 | Visual or vibration alert for deaf/HoH (screen flash, ntfy priority, wearable via push) | Android flash/vibrate [3]; Bellman pager [15] | Med | M | fits with caveats (do not claim accessibility certification) |
| 13 | Auto-lower sensitivity while device plays audio (in design) | Not found | Med | M | fits with caveats (imperfect) |
| 14 | Home Assistant / MQTT bridge (in design: MQTT later) | Frigate and AudioClassifier-MQTT [17][18] | Med | M | fits |
| 15 | Per-user quiet hours | Common in apps | Med | S | fits (currently a non-goal) |
| 16 | Thud/fall, gasp, moan | Apple Watch (wearable), Vayyar (radar) [13][14] | Med | M | fits with caveats (experimental label only) |
| 17 | Name or keyword spotting | Alexa/HA wake-word stacks | Med | L | fits with caveats (on-device model, deferred) |
| 18 | Audio clip review before alert (Nest) | Nest [5] | Med | M | conflicts (audio leaves home) |
| 19 | Emergency call/911 or monitoring center | Nest, Alexa, Apple Watch, SimpliSafe/ADT [5][6][9][13] | High | L | conflicts (claims emergency reliability, costs money) |
| 20 | Radar or camera fall detection | Vayyar [14] | Med | L | conflicts (hardware cost, medical-adjacent) |
| 21 | Paid tier or cloud AI classification | Nest, Wyze, Cubo [5][8][11] | n/a | M | conflicts ($0 rule, audio upload) |

Ranks 9-11 deliver the most user-visible value because they are the sounds people actually want; their cost is the measurement work already planned.

## 6. Positioning (resume and interview angle)

Home Awareness is not a smoke detector, security alarm, baby monitor or medical device, and it says so. It is a household notification layer for sounds you might miss, built on an on-device classifier (YAMNet) so only small events (type, confidence, time) reach the server. Its distinct angle against Apple and Android sound alerts is that it works across a household: one room sensor, several people, each with their own sounds, repeat rules, cooldowns and channels, including an alarm through the headphones they are wearing. Against Nest, Alexa, Wyze and baby-monitor subscriptions it costs $0 and sends no audio. Against Home Assistant and Frigate it is a multi-tenant product with sign-up, invites, pairing and tests proving household isolation, rather than a single-home hobby setup. The engineering story is the point: FastAPI, async SQLAlchemy, Redis rate limits and merge windows, background delivery with retries, WebSockets, per-device hashed keys, and a measured accuracy table. Avoid claims of reliability, speed of delivery or "protection". Accuracy numbers must be your own measurements.

## 7. Could not verify

- Current official prices for Nest Aware, Alexa Guard, Wyze Cam Plus, Cubo AI, Alexa Together, Vayyar Care.
- Where Nest, Alexa and Wyze run sound detection (device vs cloud) and exactly what audio they keep.
- Whether Wyze smoke-alarm detection requires a subscription.
- Any competitor doing user-defined repeat counts or headphone-routed alarms (absence of evidence only).
- Scrypted audio features; Sonic Alert pricing; Bellman pricing and independent reviews.
- Quantified false-alarm rates for any product; the Trustpilot Nanit score covers one regional page.
- Bellman's claim that NFPA/NAD recommend visual and tactile alerts.

## Sources (search results, not all opened in full)

[1] Apple Sound Recognition overviews: https://www.applemust.com/?p=16210 ; https://www.cultofmac.com/?p=715023 ; Apple dev forum (no API): https://developer.apple.com/forums/thread/761287
[2] https://www.idropnews.com/gallery/7-hidden-iphone-features-youll-wish-you-found-sooner/260516/3
[3] Android Sound Notifications: https://thenextweb.com/news/google-will-now-alert-you-of-household-sounds-through-android-notifications ; https://www.androidcentral.com/running-water-or-smoke-alarm-your-android-phone-will-now-alert-you ; https://mcmw.abilitynet.org.uk/how-to-receive-alerts-when-important-sounds-are-detected-around-you-in-android-16
[4] https://androidcentral.com/live-transcribe-will-soon-show-you-sounds-and-allow-you-save-text
[5] Nest Aware: https://voicebot.ai/2020/05/13/google-nest-aware-adds-suspicious-sound-alerts-drops-subscription-and-device-prices ; https://9to5google.com/2025/07/16/nest-aware-price-2025/ ; https://subger.com/en/service/nest-aware
[6] Alexa Guard / Ring: https://www.tomsguide.com/news/alexa-guard-plus-turns-your-amazon-echo-into-a-cheap-diy-home-security-system ; https://www.techradar.com/news/rings-latest-alarm-add-on-can-detect-the-sound-of-breaking-glass ; https://appleinsider.com/articles/20/09/24/alexa-guard-plus-expands-home-audio-surveillance-for-499-per-month
[7] https://cordcuttersnews.com/once-free-alexa-guard-features-will-now-cost-you-6-a-month/
[8] Wyze: https://www.wyze.com/pages/wyze-services-comparison ; https://forums.wyze.com/t/wyze-smoke-co-detector/41758?page=10 ; https://www.security.org/security-cameras/wyze/review/
[9] SimpliSafe: https://simplisafe.com/blog/glass-break-sensors ; https://support.simplisafe.com/conversations/alarm-sensors/glass-break-sensor-false-alarm-why/6190c64d8ea41ebb0620705b
[10] ADT: https://www.adt.com/blu/shop/glass-break-sensor ; https://security.org/home-security-systems/glass-break-sensor
[11] Baby monitors: https://ca.trustpilot.com/review/nanit.com ; https://www.productreview.com.au/listings/cubo-ai-plus-smart-baby-monitor ; https://teachtoddler.com/blog/are-smart-baby-monitors-worth-it
[12] Owlet: https://www.goodmorningamerica.com/news/story/owlet-stops-selling-baby-monitoring-smart-socks-fda-81465512 ; https://apps.apple.com/app/owlet/id1590590105
[13] Apple Watch: https://support.apple.com/en-ke/HT208944
[14] Vayyar/Walabot: https://vayyar.com/care-docs/b2c/ ; https://www.engadget.com/2018/10/24/walabot-home-fall-detection/ ; https://eastersealstech.com/2022/02/24/vayyar-care-fall-detection-device
[15] Bellman: https://us.bellman.com/pages/alerting-signaling-devices ; https://www.podfeet.com/blog/2024/04/csun-2024-bellman-symfon/
[16] Sonic Alert: https://www.maxiaids.com/sonic-alert-sonic-boom-alarm-clock-with-vibrator ; https://www.diglo.com/sonic-alert-traditional-system-ss1200c-signaling-combo;sku=SA-SS1200C%2FB;s=323;p=SA-SS1200C%2FB
[17] Frigate audio: https://docs.frigate.video/configuration/audio_detectors ; YAMNet: https://www.tensorflow.org/hub/tutorials/yamnet
[18] https://awesome.ecosyste.ms/projects/github.com%2Fc99koder%2FAudioClassifier-MQTT
[19] https://github.com/knoop7/Ava-Pro/wiki/Audio-Event-Detection ; https://wiki.seeedstudio.com/sound_event_detection_module/
[20] Apple Sound Recognition criticism: https://sixcolors.com/?p=21100 ; https://www.cultofmac.com/774171/sound-recognition-on-iphone ; https://discussions.apple.com/thread/255891226
