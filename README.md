
# ASKAP–MWA FRB Rapid-Response Triggering Project  
**Project Summary & Implementation Plan**

---

## 1. Project Overview

Fast Radio Bursts (FRBs) detected by ASKAP can benefit significantly from rapid, low-frequency follow-up observations with the Murchison Widefield Array (MWA). With the recent commissioning of the CRACO coherent pipeline, ASKAP is now capable of producing low-latency, well-localised FRB candidates suitable for triggering rapid-response observations.

This project aims to establish a reliable, low-latency triggering pathway from ASKAP/CRACO FRB candidates to MWA observations.

## 2. Architecture
### 2.1 Design
```
UDP → Queue → HTTP trigger
```

### 2.2 Components

**UDP Receiver Thread**

- Listens on configured UDP port
- Parses candidate lines (CAND_FORMAT)
- Enqueues parsed tasks
- Never performs HTTP

**HTTP Worker Thread**

- Dequeues candidate tasks
- Converts cand_mjd → GPS seconds

- Builds TriggerBuffer URL

- Sends HTTP GET request

This prevents HTTP latency from blocking UDP reception.

## 3. Candidate Format

The incoming UDP packet must follow:
```
%0.2f %lu %0.4f %d %d %0.2f %d %0.9f\n
```

Meaning:

| Field           | Description                          |
|-----------------|--------------------------------------|
| sn              | Signal-to-noise ratio                |
| tfile           | Sample number from file start        |
| time_from_file  | Seconds from file start              |
| ibc             | Boxcar width                         |
| idt             | DM trial index                       |
| dm              | Dispersion measure (pc/cm³)          |
| ibeam           | Beam number                          |
| cand_mjd        | Candidate time (MJD)                 |


Note: RA/Dec are not included. TriggerBuffer does not require sky position.

## 3. MWA TriggerBuffer Integration

Based on the official documentation:

https://mwatelescope.atlassian.net/wiki/spaces/MP/pages/24972656/Triggering+web+services

TriggerBuffer requires:
- project_id
- secure_key
- start_time (GPS seconds)
- obstime (seconds)

Pointing direction and frequency setup are inherited from the currently running MWA observation.

This script does **NOT** change MWA pointing.

## 4. Configuration
### 4.1. Set Secure Key (Required)
``` bash
export TRIGGER_SECURE_KEY="your_secret_here"
```

The secure key is never stored in code.

### 4.2 Usage

Example:
```bash
python3 udp_to_triggerbuffer.py 224.1.1.1:4900 \
  --endpoint http://127.0.0.1:8080/trigger \
  --project-id C001 \
  --past-seconds 120 \
  --obstime 600 \
  --use-start-zero \
  --workers 1 \
  -v
```
Note: For local test, need to add loopback
``` bash
sudo ip route add 224.1.1.1/32 dev lo
```
### 4.3 Parameters

#### Required

| Argument   | Description        |
|------------|-------------------|
| host:port  | UDP bind address  |

---

#### Important

| Argument | Description |
|----------|------------|
| --endpoint | TriggerBuffer URL |
| --project-id | MWA project ID |
| --past-seconds | Seconds before candidate time to dump |
| --obstime | Seconds to capture into the future |
| --use-start-zero | Use start_time=0 (recommended for continued capture triggers) |

---

#### Optional

| Argument | Description |
|----------|------------|
| --workers | Number of HTTP worker threads |
| --min-trigger-interval | Minimum seconds between triggers |
| --pretend | Dry-run mode |
| --pretty | Pretty JSON output from MWA |
| --retries | HTTP retry attempts |
| --verbose | Enable debug logging |

### Deployment Recommendation

1. Run as a systemd service on a dedicated node.

Example service file:
```ini
[Unit]
Description=ASKAP to MWA Trigger Bridge
After=network.target

[Service]
User=trigger
WorkingDirectory=/opt/askap-mwa-trigger
Environment=TRIGGER_SECURE_KEY=your_secret_here
ExecStart=/opt/venv/bin/python udp_to_triggerbuffer.py 224.1.1.1:4900 --endpoint http://127.0.0.1:8080/trigger --project-id C001 --past-seconds 120 --obstime 600 --use-start-zero --workers 1 
Restart=always

[Install]
WantedBy=multi-user.target
```

2. Docker

For deployment:
``` bash
sudo docker build -t askap-mwa-trigger:latest .
sudo docker run --rm -it   --network host   -e TRIGGER_SECURE_KEY="IAmASecret"
```
For test the docker:
```bash
sudo docker run --rm -it   --network host   -e TRIGGER_SECURE_KEY="IAmASecret"   askap-mwa-trigger:latest 224.1.1.1:4900   --endpoint http://127.0.0.1:8080/trigger   --project-id C001   --use-sta
rt-zero   --obstime 600   --workers 1   -v
```

### Assumptions

- UDP is broadcast on ASKAP internal network
- Packet loss is assumed negligible
- Candidate spacing typically >5 seconds
- MWA trigger interface uses HTTP (not HTTPS)

**Important**: The secure key is transmitted in plaintext because the MWA trigger service is HTTP-only.

### Reproduction note:
```
python==3.12.3
astropy==7.2.0
numpy==2.3.5
requests==2.32.5
urllib3==2.6.3
```