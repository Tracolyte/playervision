# camera-pipeline (MVP)

Captures one JPEG still from an RTSP camera every N seconds during operating hours,
uploads to Supabase Storage, and upserts metadata to Supabase Postgres.

## Quick start (after installing on the Pi)

1) Copy config and env
- sudo mkdir -p /etc/camera-pipeline
- sudo cp config.example.toml /etc/camera-pipeline/config.toml
- sudo cp camera-pipeline.env.example /etc/camera-pipeline/camera-pipeline.env
- sudo chmod 600 /etc/camera-pipeline/camera-pipeline.env

2) Edit /etc/camera-pipeline/camera-pipeline.env with:
- SUPABASE_URL
- SUPABASE_SERVICE_ROLE_KEY
- CAMERA_RTSP_URL

3) Test one capture
/opt/camera-pipeline/venv/bin/python -m camera_pipeline.main --config /etc/camera-pipeline/config.toml --once

4) Run as a service
sudo cp deploy/camera-pipeline.service /etc/systemd/system/camera-pipeline.service
sudo systemctl daemon-reload
sudo systemctl enable --now camera-pipeline

5) View logs
journalctl -u camera-pipeline -f
