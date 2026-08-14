# AceStream web player

A small browser-based player for an AceStream engine running with Docker
Compose. The web interface is available on port `8080` and proxies streams from
the engine on port `6878`.

## Requirements

- A 64-bit Raspberry Pi OS installation using 4 KB memory pages
- Docker Engine with the Docker Compose plugin
- `systemd`
- Network access for pulling the container images

Confirm that the kernel uses 4 KB pages:

```bash
getconf PAGESIZE
```

The expected result is `4096`.

## Run manually

From the project directory:

```bash
docker compose up --detach --build
```

Open `http://raspberrypi.local:8080/` in a browser. To stop the application:

```bash
docker compose down
```

## Install as a system service

The included [`acestream.service`](./acestream.service) expects the project to
be installed at `/opt/acestream`. Run these commands from the project root on
the Pi:

```bash
sudo mkdir -p /opt/acestream
sudo cp -a . /opt/acestream/
cd /opt/acestream
sudo docker compose pull
sudo docker compose build player
sudo cp acestream.service /etc/systemd/system/acestream.service
sudo systemctl daemon-reload
sudo systemctl enable --now acestream.service
```

Check that the service and containers started successfully:

```bash
sudo systemctl status acestream.service
sudo docker compose ps
```

The player will now start automatically after Docker and the network are ready.

### Service commands

```bash
# Start, stop, or restart everything
sudo systemctl start acestream.service
sudo systemctl stop acestream.service
sudo systemctl restart acestream.service

# Show service activity from the current boot
sudo journalctl -u acestream.service -b

# Follow container logs
cd /opt/acestream
sudo docker compose logs --follow
```

### Update the deployment

Copy the updated project files into `/opt/acestream`, then rebuild and restart:

```bash
cd /opt/acestream
sudo docker compose pull
sudo docker compose build player
sudo systemctl restart acestream.service
```

If `acestream.service` itself changed, reinstall it before restarting:

```bash
sudo cp /opt/acestream/acestream.service /etc/systemd/system/acestream.service
sudo systemctl daemon-reload
sudo systemctl restart acestream.service
```

### Remove the service

```bash
sudo systemctl disable --now acestream.service
sudo rm /etc/systemd/system/acestream.service
sudo systemctl daemon-reload
```

This removes the systemd unit and stops the Compose application. It does not
delete `/opt/acestream` or downloaded Docker images.
