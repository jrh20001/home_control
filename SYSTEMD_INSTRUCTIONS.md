# Systemd Deployment Instructions

Follow these steps to deploy the Home Control services to your remote server.

## 1. Copy Files to Server

Transfer the service and timer files to the remote server (e.g., using `scp`):

```bash
scp hc_server.service update_crontab.service update_crontab.timer ubuntu@<remote_host>:/home/ubuntu/home_control/
```

## 2. Install Services

SSH into the remote server and move the files to the systemd directory:

```bash
ssh ubuntu@<remote_host>
sudo cp /home/ubuntu/home_control/*.service /etc/systemd/system/
sudo cp /home/ubuntu/home_control/*.timer /etc/systemd/system/
```

## 3. Enable and Start Services

Reload the systemd daemon to recognize the new files:

```bash
sudo systemctl daemon-reload
```

### Home Control Server

Enable and start the main server:

```bash
sudo systemctl enable hc_server.service
sudo systemctl start hc_server.service
```

Check status:
```bash
sudo systemctl status hc_server.service
```

### Update Crontab (Schedule)

Enable and start the timer (this will run the service daily):

```bash
sudo systemctl enable update_crontab.timer
sudo systemctl start update_crontab.timer
```

To run the update immediately (without waiting for the timer):

```bash
sudo systemctl start update_crontab.service
```

Check timer status:
```bash
sudo systemctl list-timers --all | grep update_crontab
```

## 4. Troubleshooting

View logs for the services:

```bash
# For the web server
sudo journalctl -u hc_server.service -f

# For the schedule updater
sudo journalctl -u update_crontab.service -f
```
