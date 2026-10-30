#!/usr/bin/env bash
# =============================================================================
#  Firmware mantık testleri — kart GEREKMEZ (Linux/macOS, g++ ≥ 9)
#  Gerçek node1/node2 main.cpp dosyaları taklit donanımla (mock) derlenir ve
#  senaryo çalıştırılır: ısınma → çakmak gazı → merkezden susturma → ısı →
#  radyo kesintisi → test bip → BOOT test alarmı.
#  Kullanım:  bash firmware/test_host/run.sh
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"
OUT="${TMPDIR:-/tmp}/orman_fw_test"
mkdir -p "$OUT"
g++ -std=gnu++17 -O1 -Wall -Wextra -I mock -o "$OUT/test_node1" test_node1.cpp
g++ -std=gnu++17 -O1 -Wall -Wextra -I mock -o "$OUT/test_node2" test_node2.cpp
( cd "$OUT" && ./test_node1 && ./test_node2 )
echo "Çıktılar: $OUT/node1_usb.jsonl  ·  $OUT/gateway_out.tsv"
