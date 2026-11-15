# Myo fall detection (work in progress)

Detects falls from the Myo armband's motion sensor and sends an alert to the Jetson. The Rosey agent can forward those alerts to the control server as `fall_detected` events. This adds a second signal alongside camera-based fall detection.

## Status: not yet run on real hardware

| Part | State |
|---|---|
| Fall-detection logic (`myo_fall/detector.py`) | Works on **simulated** data only: 4/4 simulated falls detected, 0 false alarms in 6 everyday activities. Thresholds still need tuning on real recordings. |
| Bluetooth client (`myo_fall/myo_ble.py`) | Written against Thalmic's published Myo protocol; **untested with a real armband** |
| Alerts over UDP + receiver (`events.py`, `jetson_receiver.py`) | Tested |
| Forwarding to the server | Done: `python3 -m rosey_agent --myo-udp 5006` (see `jetson/rosey_agent/myo_bridge.py`) |

Next steps:
1. On the Jetson, check that `python3 -m myo_fall scan` finds the armband and `run` streams data at about 50 Hz.
2. Record real sessions: supervised falls onto a crash mat plus normal daily activity.
3. Replay them, tune the thresholds, and update the defaults in `FallConfig`.
4. Add a systemd unit so it starts on boot (copy `jetson/rosey-agent.service`).

## Run it on the Jetson

```bash
cd ~/rosey/myo && python3 -m pip install --user -r requirements.txt
```

Wake the armband by moving it, make sure it isn't connected to another computer, then:

```bash
python3 -m myo_fall scan
```

Send alerts to the Rosey agent (start the agent with `--myo-udp 5006`):

```bash
python3 -m myo_fall run --udp 127.0.0.1:5006
```

Or watch alerts on their own, without the agent:

```bash
python3 -m myo_fall run --udp 127.0.0.1:5005
```

```bash
python3 jetson_receiver.py
```

The armband buzzes once when it connects and long when it detects a fall. It's set to never sleep, because by default a Myo sleeps when it stops moving, which is exactly what someone lying on the floor looks like. That drains the battery faster, so watch the battery level in the heartbeats.

If the Jetson has no working Bluetooth, use the blue BLED112 USB dongle that came with the Myo: `pip install pyomyo`, then `run --backend pyomyo`.

## Try it without the armband

```bash
python3 -m myo_fall simulate sim_data
```

```bash
python3 -m myo_fall replay sim_data --show-rejected
```

## Tuning on real data

1. Record while running: `python3 -m myo_fall run --record` saves CSVs under `recordings/`. Record one trial per file.
2. Rename files that contain a fall to `fall_<something>.csv`. Name everything else after the activity (`walking_1.csv`, `clap_2.csv` and so on).
3. Replay with different settings to compare detection rate and false alarms:

```bash
python3 -m myo_fall replay recordings --set impact_g=3.0 --set min_supporting=1
```

Only test falls with healthy volunteers onto crash mats, never with seniors.

## How detection works

When an impact spike happens (≥ 2.5 g), it waits 3 seconds and calls it a fall only if:
- the wearer is **lying still** afterwards, and
- at least **2 of 3** supporting signs are present: free fall just before the impact, fast rotation around it, or the arm ending at a very different angle to gravity.

Every impact that was checked is logged with its measurements and why it was accepted or rejected (`--show-rejected`). That's what you tune from. All settings are in `FallConfig` in `myo_fall/detector.py` and can be overridden with `--set key=value`.

Known limits: if the person gets straight back up, the fall is ignored by design. Arm-only sensing can't tell posture. Impacts are sampled at 50 Hz, so short peaks can read low.

## Alert format (UDP JSON)

```json
{"type": "fall", "id": "…", "device": "Myo", "event": {"confidence": 0.8, "peak_g": 4.9, "cues": ["rotation", "orientation"]}}
{"type": "heartbeat", "connected": true, "battery": 81, "rate_hz": 49.9, "state": "idle"}
```

Fall alerts are sent 5 times with the same `id` (UDP can drop packets); receivers de-duplicate. A heartbeat goes out every second, so if heartbeats stop, treat the sensor as offline. Silence never means "no fall".
