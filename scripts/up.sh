#!/usr/bin/env bash
# Start the Police AI dev stack. Detects Arch kernel/module mismatch
# (common cause of Docker "veth ... operation not supported" errors).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

running_kernel="$(uname -r)"

if [[ ! -d "/lib/modules/${running_kernel}" ]]; then
  installed="$(ls /lib/modules/ 2>/dev/null | tr '\n' ' ')"
  cat <<EOF
Docker kernel mismatch detected.

  Running kernel:  ${running_kernel}
  Module dirs:     ${installed:-<none>}

After a kernel update on Arch Linux, Docker cannot create container
networks until you reboot into the new kernel.

Fix:
  sudo reboot

After reboot, run:
  docker compose up -d --build
EOF
  exit 1
fi

exec docker compose up -d --build "$@"
