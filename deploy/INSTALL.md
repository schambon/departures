# Installing on the Raspberry Pi

Needs Python 3.9+ (Raspberry Pi OS Bookworm ships 3.11) and the system `tzdata` package (normally present).

## 1. Tailscale (once)
Follow https://tailscale.com/download/linux on the Pi, run `sudo tailscale up`, and install the Tailscale app on the iPhone
signed in to the same account. Then `tailscale ip -4` on the Pi prints its address (100.x.y.z).

## 2. Copy and unpack (from the laptop)
    scp dist/departures.tar.gz pi@<pi-address>:/tmp/
    ssh pi@<pi-address>
    sudo mkdir -p /opt/departures && sudo tar -xzf /tmp/departures.tar.gz -C /opt/departures --strip-components=1

## 3. Service user and secret
    sudo useradd --system --no-create-home --shell /usr/sbin/nologin departures
    sudo cp /opt/departures/deploy/departures.env.example /etc/departures.env
    sudo chmod 600 /etc/departures.env && sudo chown root:root /etc/departures.env
    sudo nano /etc/departures.env          # set PRIM_API_KEY and BIND_HOST

## 4. Test by hand
    sudo -u departures env $(sudo cat /etc/departures.env | xargs) python3 /opt/departures/departures.py
Expect the departures list. (Port 8080 is free by default; Pi-hole uses 80/53, check with `ss -ltnp` if unsure.)

## 5. Start at boot
    sudo cp /opt/departures/deploy/departures.service /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now departures
    systemctl status departures            # logs: journalctl -u departures -f

## 6. Use it
On the iPhone, with Tailscale connected, open http://<BIND_HOST>:8080/ in Safari and use Share, Add to Home Screen.

## 7. Weekday notification at 07:20 (optional)
In Shortcuts, create a shortcut with two actions: Get Contents of URL (`http://<BIND_HOST>:8080/next.txt`) then Show Notification
with that result. Then Automation, New Automation, Time of Day: 07:20, Weekly, Monday to Friday, Run Shortcut, Run Immediately.
Tailscale must be connected on the phone at that time.

Tests (optional): `cd /opt/departures && python3 -m unittest discover -s tests`
