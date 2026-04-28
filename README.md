
# ASKAP–MWA FRB Rapid-Response Triggering

Receives CRACO/snoopy FRB candidates over UDP and forwards them as HTTP GET triggers to the MWA TriggerBuffer service.

---

## 1. Architecture

```
CRACO/snoopy (UDP multicast)
        │
        ▼
udp_to_triggerbuffer.py
  ├─ UDP Receiver Thread   — receives & parses candidate packets, enqueues tasks
  ├─ Filter Layer          — SNR / DM thresholds, burst suppression, rate limit
  └─ HTTP Worker Thread(s) — dequeues, builds TriggerBuffer URL, sends GET request
        │
        ▼
MWA TriggerBuffer (HTTP)
```

The UDP receiver and HTTP worker run in separate threads so that HTTP latency never blocks candidate reception.

---

## 2. Candidate Format

Incoming UDP packets must follow the 8-column snoopy format:

```
%0.2f %lu %0.4f %d %d %0.2f %d %0.9f\n
```

| Field          | Type  | Description                    |
|----------------|-------|--------------------------------|
| sn             | float | Signal-to-noise ratio          |
| tfile          | int   | Sample number from file start  |
| time_from_file | float | Seconds from file start        |
| ibc            | int   | Boxcar width                   |
| idt            | int   | DM trial index                 |
| dm             | float | Dispersion measure (pc/cm³)    |
| ibeam          | int   | Beam number                    |
| cand_mjd       | float | Candidate time (MJD)           |

RA/Dec are not included. TriggerBuffer does not require sky position — pointing is inherited from the currently running MWA observation.

---

## 3. MWA TriggerBuffer Integration

See: https://mwatelescope.atlassian.net/wiki/spaces/MP/pages/24972656/Triggering+web+services

TriggerBuffer requires:
- `project_id`
- `secure_key`
- `start_time` (GPS seconds)
- `obstime` (seconds)

This script does **not** change MWA pointing direction or frequency setup.

---

## 4. Quick Start

### 4.1 Set Secure Key (required)

```bash
export TRIGGER_SECURE_KEY="your_secret_here"
```

The key is read from the environment at runtime and is never stored in code.

### 4.2 Run

```bash
python3 udp_to_triggerbuffer.py 224.1.1.1:4900 \
  --endpoint http://mro.mwa128t.org/trigger/triggerbuffer \
  --project-id C001 \
  --past-seconds 120 \
  --obstime 600 \
  --use-start-zero \
  --min-sn 20.0 \
  --min-dm 100.0 \
  --burst-window 1.0 \
  --burst-max-count 10 \
  --pretend \
  -v
```

For local testing, add a loopback route for the multicast address:

```bash
sudo ip route add 224.1.1.1/32 dev lo
```

---

## 5. Parameters

### Required

| Argument    | Description              |
|-------------|--------------------------|
| `host:port` | UDP bind address (e.g. `224.1.1.1:4900`) |

### MWA Trigger

| Argument            | Default                                        | Description                                          |
|---------------------|------------------------------------------------|------------------------------------------------------|
| `--endpoint`        | `http://mro.mwa128t.org/trigger/triggerbuffer` | TriggerBuffer base URL                               |
| `--project-id`      | `C001`                                         | MWA project ID                                       |
| `--past-seconds`    | `120`                                          | Seconds before candidate time to start buffer dump   |
| `--obstime`         | `600`                                          | Seconds of MWA capture                               |
| `--use-start-zero`  | off                                            | Use `start_time=0` (recommended for buffer triggers) |
| `--pretend`         | off                                            | Dry-run — sends request but MWA will not record      |
| `--pretty`          | off                                            | Request pretty-printed JSON response from MWA        |

### Filtering

| Argument              | Default | Description                                                                 |
|-----------------------|---------|-----------------------------------------------------------------------------|
| `--min-sn`            | `20.0`  | Minimum SNR to forward a trigger. Candidates below this are discarded.      |
| `--min-dm`            | `0.0`   | Minimum DM (pc/cm³) to forward a trigger. Set based on science requirement. |
| `--burst-window`      | `1.0`   | Time window (seconds) for burst detection.                                  |
| `--burst-max-count`   | `10`    | Max candidates within `--burst-window` before suppressing — satellite RFI indicator. |
| `--min-trigger-interval` | `2.0` | Minimum seconds between consecutive MWA triggers (0 disables).           |

**Filter order:** SNR → DM → burst suppression → rate limit → send trigger.

### Other

| Argument              | Default | Description                              |
|-----------------------|---------|------------------------------------------|
| `--workers`           | `1`     | Number of HTTP worker threads            |
| `--queue-size`        | `1000`  | Max queued UDP candidates                |
| `--retries`           | `2`     | HTTP retry attempts on failure           |
| `--timeout-connect`   | `2.0`   | HTTP connect timeout (seconds)           |
| `--timeout-read`      | `10.0`  | HTTP read timeout (seconds)              |
| `--debug-url`         | None    | Forward all parsed candidates to this URL (for monitoring) |
| `--secure-key-env`    | `TRIGGER_SECURE_KEY` | Environment variable name for secure key |
| `-v / --verbose`      | off     | Enable per-candidate debug logging       |

---

## 6. Debug Receiver

`receiver/receiver.py` is a companion HTTP server that receives debug events forwarded by `--debug-url`. It logs all candidates to JSONL and CSV for inspection.

```bash
python3 receiver/receiver.py --port 8080
```

Logs are written to `receiver/data/candidates.jsonl`, `receiver/data/candidates.csv`, and `receiver/log/access.log`.

---

## 7. Deployment

### systemd

```ini
[Unit]
Description=ASKAP to MWA FRB Trigger Bridge
After=network.target

[Service]
User=trigger
WorkingDirectory=/opt/askap-mwa-trigger
Environment=TRIGGER_SECURE_KEY=your_secret_here
ExecStart=/opt/venv/bin/python3 udp_to_triggerbuffer.py 224.1.1.1:4900 \
    --endpoint http://mro.mwa128t.org/trigger/triggerbuffer \
    --project-id C001 \
    --past-seconds 120 \
    --obstime 600 \
    --use-start-zero \
    --min-sn 20.0 \
    --min-dm 100.0 \
    --burst-window 1.0 \
    --burst-max-count 10 \
    --workers 1
Restart=always

[Install]
WantedBy=multi-user.target
```

### Docker

```bash
# Build
sudo docker build -t askap-mwa-trigger:latest .

# Run
sudo docker run --rm -it \
  --network host \
  -e TRIGGER_SECURE_KEY="your_secret_here" \
  askap-mwa-trigger:latest 224.1.1.1:4900 \
  --endpoint http://mro.mwa128t.org/trigger/triggerbuffer \
  --project-id C001 \
  --use-start-zero \
  --obstime 600 \
  --min-sn 20.0 \
  --min-dm 100.0 \
  --pretend \
  -v
```

---

## 8. Testing

Unit tests for the filter layer:

```bash
python3 test_filters.py
```

Covers SNR threshold, DM threshold, burst suppression, and window expiry.

---

## 9. Notes

- UDP is broadcast on the ASKAP internal multicast network; packet loss is assumed negligible.
- The MWA trigger interface uses HTTP (not HTTPS). The secure key is transmitted in plaintext — this is a limitation of the current MWA trigger service.
- `--min-sn` and `--min-dm` thresholds are placeholder defaults and should be set based on science input from the CRAFT team.

---

## 10. Dependencies

```
python >= 3.12
astropy >= 7.2.0
numpy >= 2.3.5
requests >= 2.32.5
```
