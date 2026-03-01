````markdown
# PlayerVision Ethernet Camera + Wi-Fi Uplink (Home Test) — What We Changed

Goal: run the existing PlayerVision pipeline with the camera on a **private Ethernet link** (Pi ↔ Reolink E1 Pro) while the Pi uses **Wi-Fi** for internet/Supabase.  
Result: confirmed working end-to-end (DHCP lease to camera, RTSP capture succeeds, still.jpg written).

---

## High-level network architecture (what we implemented)

- **wlan0 (home Wi-Fi)**: normal home LAN + **default route** for internet (Supabase).
- **eth0 (camera-only Ethernet)**: isolated subnet **192.168.50.0/24** that exists only between the Pi and the camera.
- Pi acts as:
  - **static IP gateway/anchor** on eth0: `192.168.50.1/24`
  - **DHCP server** for devices on eth0 (the camera)
- Camera receives a private IP from the Pi and serves RTSP over that link.

Key invariant:
- **Default route must remain on wlan0**, and eth0 must be a link-only subnet (no internet gateway).

---

## Changes on the Raspberry Pi (networking)

### 1) Confirmed NetworkManager is the network stack (not dhcpcd)
Commands used:
- `sudo systemctl is-active NetworkManager`
- `sudo systemctl is-active dhcpcd`
- `nmcli device status`

Observation:
- NetworkManager was active; dhcpcd inactive.
- eth0 was originally managed via a connection named `netplan-eth0` and was attempting DHCP (`ipv4.method: auto`).

### 2) Reconfigured eth0 (`netplan-eth0`) to be a static camera-only subnet

We **modified** the existing NetworkManager connection `netplan-eth0`:

- Set eth0 static IP:
  - Pi eth0 address: `192.168.50.1/24`
- Disabled IPv6 on eth0
- Ensured eth0 never becomes default route:
  - `ipv4.never-default yes`

Command applied:
- `sudo nmcli con mod netplan-eth0 ipv4.method manual ipv4.addresses 192.168.50.1/24 ipv4.never-default yes ipv6.method ignore`

Then forced re-activation:
- `sudo nmcli device disconnect eth0`
- `sudo nmcli con up netplan-eth0`

Verified:
- `ip -4 -br addr show dev eth0` → `192.168.50.1/24`
- `ip route` contains:
  - `default via 192.168.1.1 dev wlan0 ...`
  - `192.168.50.0/24 dev eth0 ...`
- `ip route get 1.1.1.1` → `dev wlan0`

### 3) (Optional hardening) Enabled Ethernet autonegotiation on eth0
Reason: earlier connection dump showed `802-3-ethernet.auto-negotiate: no`.  
This can cause interoperability issues with some devices/cables.

Command suggested/applied:
- `sudo nmcli con mod netplan-eth0 802-3-ethernet.auto-negotiate yes`
- `sudo nmcli con down netplan-eth0 && sudo nmcli con up netplan-eth0`

Validation tool:
- `sudo ethtool eth0` (confirmed link detected)

---

## Changes on the Raspberry Pi (DHCP on eth0 via dnsmasq)

### 4) Installed dnsmasq
- `sudo apt-get install -y dnsmasq`

### 5) Created dnsmasq config to serve DHCP **only on eth0**
File created:
- `/etc/dnsmasq.d/cam-net.conf`

Initial content (dynamic pool):
```ini
interface=eth0
bind-interfaces
dhcp-range=192.168.50.10,192.168.50.50,255.255.255.0,24h
````

Restarted + verified:

* `sudo systemctl restart dnsmasq`
* `sudo systemctl status dnsmasq --no-pager`
* `sudo journalctl -u dnsmasq -n 50 --no-pager -o cat`

dnsmasq logs confirmed:

* “DHCP, sockets bound exclusively to interface eth0”

### 6) Confirmed camera obtained DHCP lease and IP on eth0

Observed lease activity in dnsmasq logs:

* `DHCPDISCOVER(eth0) ...`
* `DHCPOFFER(eth0) 192.168.50.32 ...`
* `DHCPACK(eth0) 192.168.50.32 ... playervisioncam`

We checked:

* `/var/lib/misc/dnsmasq.leases`
* `ip neigh show dev eth0`
* `ping -c 3 192.168.50.32` (success)

### 7) Pinned camera to a stable IP (MAC reservation)

Reason: pipeline needs a stable RTSP endpoint; dynamic DHCP IP can change.

Updated `/etc/dnsmasq.d/cam-net.conf` to include:

* MAC reservation:

  * camera MAC: `ec:71:db:c5:4b:1b`
  * pinned IP: `192.168.50.10`

Final content:

```ini
interface=eth0
bind-interfaces

# Reserve Reolink camera by MAC
dhcp-host=ec:71:db:c5:4b:1b,192.168.50.10,playervisioncam,24h

# Dynamic pool for anything else that plugs into eth0
dhcp-range=192.168.50.20,192.168.50.50,255.255.255.0,24h
```

Restarted dnsmasq and confirmed lease moved to .10:

* `/var/lib/misc/dnsmasq.leases` showed `192.168.50.10`
* `ping 192.168.50.10` succeeded

### 8) Fixed a dnsmasq config pitfall: backups in `/etc/dnsmasq.d/` are loaded

We initially created a backup file in `/etc/dnsmasq.d/`:

* `/etc/dnsmasq.d/cam-net.conf.bak.<timestamp>`

dnsmasq reads **all files** in that directory (not only `*.conf`), so it loaded both configs and logged two DHCP ranges.

Fix:

* Created a disabled folder:

  * `/etc/dnsmasq.d/disabled/`
* Moved the backup file(s) out of `/etc/dnsmasq.d/`:

  * `sudo mv /etc/dnsmasq.d/cam-net.conf.bak.* /etc/dnsmasq.d/disabled/`

Verified with:

* `sudo grep -RIn 'dhcp-range' /etc/dnsmasq.conf /etc/dnsmasq.d`
* `sudo journalctl -u dnsmasq --since "2 minutes ago" --no-pager -o cat`

  * confirmed only one range line remains: `192.168.50.20 -- 192.168.50.50`

### 9) Limited dnsmasq DNS binding + reduced DHCP log noise (optional)

We added:

* `/etc/dnsmasq.d/cam-net-extra.conf`

Content:

```ini
# Only answer DNS on localhost + camera subnet
listen-address=127.0.0.1,192.168.50.1

# Reduce log noise (DHCP assignments still appear)
quiet-dhcp
```

Verified listening sockets:

* `sudo ss -lntup | grep dnsmasq`

  * bound to 127.0.0.1:53 and 192.168.50.1:53; DHCP on :67.

---

## RTSP validation (proved the pipeline capture method works over eth0)

### 10) Verified RTSP works on camera’s eth0 IP

Camera RTSP endpoint used:

* `rtsp://admin:playervision2026@192.168.50.10:554/Preview_01_sub`

We validated using ffprobe/ffmpeg:

```bash
ffprobe -rtsp_transport tcp -timeout 7000000 \
  -i "rtsp://admin:playervision2026@192.168.50.10:554/Preview_01_sub"

ffmpeg -rtsp_transport tcp -timeout 7000000 \
  -i "rtsp://admin:playervision2026@192.168.50.10:554/Preview_01_sub" \
  -frames:v 1 -q:v 2 -y /tmp/camtest/still.jpg
```

Result:

* `still.jpg` was successfully created (confirmed via `ls -lh`).

Note:

* ffmpeg printed `Overread VUI by 8 bits` warnings; capture still succeeded (non-fatal).

---

## Changes to PlayerVision pipeline configuration (env only)

### 11) Updated CAMERA_RTSP_URL to point to eth0 camera IP

We changed the env var in:

* `/etc/camera-pipeline/camera-pipeline.env`

New value:

* `CAMERA_RTSP_URL="rtsp://admin:playervision2026@192.168.50.10:554/Preview_01_sub"`

We also created a backup (important):

* `/etc/camera-pipeline/camera-pipeline.env.bak.<timestamp>`

Verification:

* `sudo grep -n '^CAMERA_RTSP_URL=' /etc/camera-pipeline/camera-pipeline.env`

---

## FFmpeg timeout flag compatibility (repo-level note)

### 12) Confirmed pipeline uses the correct FFmpeg flag on this OS image

We discovered:

* ffprobe/ffmpeg on this Pi do **not** support `-stimeout`.
* They do support:

  * `-timeout` (socket I/O timeout; microseconds in this context)
  * `-rw_timeout` (I/O operations timeout; int64 microseconds)

Repo check showed pipeline is already correct:

* `camera_pipeline/capture.py` uses: `"-timeout", str(stimeout_us)`

So: **no code changes were required**, but the config field name `ffmpeg_stimeout_us` is now “historically named” (it maps to ffmpeg `-timeout`, not `-stimeout`).

Recommendation (optional refactor):

* Rename config key/field:

  * `ffmpeg_stimeout_us` → `ffmpeg_timeout_us`
  * Update TOML key + dataclasses + scheduler wiring accordingly.

---

## Summary of new “Ethernet camera + Wi-Fi uplink” requirements for deployment

1. Pi must be dual-homed:

   * wlan0: internet default route
   * eth0: private camera subnet (no default)
2. Pi must run DHCP on eth0 (dnsmasq):

   * camera pinned to a stable IP via MAC reservation
3. Pipeline configuration:

   * RTSP URL points to the camera’s eth0 IP (e.g., 192.168.50.10)
4. FFmpeg invocation:

   * must use `-timeout` (not `-stimeout`) on this OS image

This setup is directly transferable to University Wi-Fi:

* The camera never touches campus Wi-Fi.
* The Pi uploads to Supabase over campus Wi-Fi.
* The capture loop uses RTSP over the private eth0 link.

```
```
