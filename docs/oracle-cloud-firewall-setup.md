# Oracle Cloud Firewall Setup for Port 80

Opening a port on OCI requires unblocking **two layers** of firewall:
1. **OCI Security List** — cloud-level network rules
2. **Instance iptables** — OS-level rules pre-configured by Oracle's Ubuntu image

---

## Layer 1: OCI Security List

By default, only SSH (port 22) is allowed inbound. Port 80 must be added manually.

### Steps

1. Log into the OCI console
2. Go to **Networking → Virtual Cloud Networks → askap-trigger-vcn**
3. Click **Security Lists** in the left menu
4. Click **Default Security List for askap-trigger-vcn**
5. Click **Add Ingress Rules** and fill in:

| Field | Value |
|-------|-------|
| Stateless | No |
| Source Type | CIDR |
| Source CIDR | `0.0.0.0/0` |
| IP Protocol | TCP |
| Source Port Range | (leave blank — All) |
| Destination Port Range | `80` |
| Description | HTTP traffic |

6. Click **Add Ingress Rules** to save

### Verify the rule was saved

The Ingress Rules list should now contain:

```
No  0.0.0.0/0  TCP  All  80  TCP traffic for ports: 80
```

> **Note:** The security list must be bound to the subnet your instance is in.
> Check: **Compute → Instances → instance → Attached VNICs** to confirm the subnet.
> Public subnet instances use the `Default Security List`.
> Private subnet instances use `security list for private subnet-*`.

---

## Layer 2: Instance iptables

OCI's Ubuntu image ships with iptables rules that only allow traffic to internal metadata addresses (169.254.x.x). **Inbound traffic from the internet is dropped by default**, even after the Security List is updated.

### Open port 80

```bash
sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
```

### Persist across reboots

```bash
sudo apt install iptables-persistent -y
sudo netfilter-persistent save
```

Rules are saved to `/etc/iptables/rules.v4` and loaded automatically on boot.

---

## Verification

### Check the service is listening on all interfaces

```bash
sudo ss -tlnp | grep :80
```

Expected output (must show `0.0.0.0:80`, not `127.0.0.1:80`):

```
LISTEN 0  5  0.0.0.0:80  0.0.0.0:*  users:(("python",pid=...,fd=3))
```

### Test locally on the instance

```bash
curl http://127.0.0.1
```

### Test from an external machine

```bash
curl http://<public-ip>
# or
nc -zv <public-ip> 80
```

> **Note:** OCI's public IP is NAT-mapped. Accessing your own public IP from inside the instance will fail — this is expected and does not affect external access.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Connection times out | Security List rule missing | Add Ingress Rule for port 80 |
| `Connection refused` | iptables blocking inbound traffic | `sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT` |
| `Connection refused` | No service listening on port 80 | Start your application |
| Local OK, external fails | iptables blocking inbound traffic | Same as above |
| Rules lost after reboot | iptables not persisted | Install `iptables-persistent` and save |

---

## VCN Security List Overview

A typical VCN has two security lists:

| Security List | Bound Subnet | Purpose |
|---------------|-------------|---------|
| Default Security List | Public subnet | Allows public internet access (SSH, HTTP, etc.) |
| Security List for Private Subnet | Private subnet | Only allows traffic within the VCN |

Instances that need public internet access should be in the public subnet, with rules added to the Default Security List.

---

## Default Security List — Current Ingress Rules

After setup, the Default Security List should contain:

| Stateless | Source | Protocol | Dest Port | Description |
|-----------|--------|----------|-----------|-------------|
| No | 0.0.0.0/0 | TCP | 22 | SSH |
| No | 0.0.0.0/0 | ICMP 3,4 | — | Path MTU discovery |
| No | 10.0.0.0/16 | ICMP 3 | — | VCN internal unreachable |
| No | 0.0.0.0/0 | TCP | **80** | **HTTP (added manually)** |
