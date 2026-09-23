#!/usr/bin/env bash
set -euo pipefail

# The Espressif image initializes the SDK from its own entrypoint. Our image
# replaces that entrypoint, so initialize the same environment explicitly.
# shellcheck disable=SC1090
source "${IDF_PATH}/export.sh" >/dev/null

if [[ "${FIRMWARE_BUILDER_MODE:-job}" == "server" ]]; then
  exec python3 "${FIRMWARE_SOURCE_DIR}/docker/firmware-builder/server.py"
fi
exec python3 "${FIRMWARE_SOURCE_DIR}/docker/firmware-builder/firmware_builder.py" "$@"

