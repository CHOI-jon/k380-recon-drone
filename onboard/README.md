# onboard — 라즈베리파이 4 탐지 노드

| 파일 | 설명 |
|---|---|
| `detector_node.py` | 카메라 → YOLOv8n(NCNN) 추론 → 탐지 박스·영상·프레임 시각 자세를 지상국으로 송신 (v3.4 — 원본 녹화, 셔터 고정·선명 프레임 선택, 성능 기록) |
| `detector.env` | 자동 실행 시 사용할 모델·입력 크기·옵션 |
| `install_autostart.sh` | 부팅 시 `detector_node.py`가 자동 실행되도록 systemd 서비스 등록 |
| `requirements-pi.txt` | 파이 쪽 파이썬 패키지 |

파이에서는 `~/pi/` 에 위 파일을, `~/vision/models/` 에 `models/best416_ncnn` 등을 두고 실행합니다.
FC(KOAFC) 연결은 UART + mavlink-router(UDP 14551로 중계)를 사용합니다.
