#!/bin/bash
# 파이 전원이 들어오면 detector_node.py 가 자동으로 켜지게 설정 (한 번만 실행)
#   bash ~/pi/install_autostart.sh
set -e
U=$(whoami)
H=$HOME
PY=$H/visionenv/bin/python3
[ -x "$PY" ] || { echo "가상환경 $PY 없음"; exit 1; }
[ -f "$H/pi/detector_node.py" ] || { echo "$H/pi/detector_node.py 없음"; exit 1; }
[ -f "$H/pi/detector.env" ] || { echo "$H/pi/detector.env 없음 (노트북에서 같이 복사)"; exit 1; }
sed -i "s#/home/jong#$H#g" "$H/pi/detector.env"
mkdir -p "$H/rec"

sudo tee /etc/systemd/system/detector.service >/dev/null <<EOF
[Unit]
Description=AI recon drone detector_node (camera + YOLO + MAVLink)
After=network-online.target mavlink-router.service
Wants=network-online.target

[Service]
User=$U
WorkingDirectory=$H
EnvironmentFile=$H/pi/detector.env
Environment=PYTHONUNBUFFERED=1
ExecStartPre=/bin/sleep 3
ExecStart=/bin/bash -c 'exec $PY -u $H/pi/detector_node.py --model $H/vision/models/\${MODEL} --imgsz \${IMGSZ} --threads \${THREADS} --mavlink udpin:127.0.0.1:14551 \${EXTRA}'
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

pkill -f detector_node.py 2>/dev/null || true
sudo systemctl daemon-reload
sudo systemctl enable --now detector.service
echo "설정 완료. 10초 뒤 상태를 확인합니다..."
sleep 10
systemctl --no-pager --lines=15 status detector.service || true
