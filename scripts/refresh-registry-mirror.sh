#!/usr/bin/env bash
# Refresh the local registry mirror (localhost:5000) with the latest GHCR
# images. The mirror is a static cache, not a pull-through proxy: once a tag
# exists locally, k3s never re-checks GHCR for it, so this must run after any
# new publish for the cluster to actually pick it up.
set -euo pipefail

IMAGES=(
  "fabceolin/unlimited-ocr-container:gpu"
  "fabceolin/whisper-diarization-container:gpu"
  "fabceolin/whisper-fast-container:gpu"
)

for image in "${IMAGES[@]}"; do
  echo "=== $image ===" >&2
  docker pull "ghcr.io/$image"
  docker tag "ghcr.io/$image" "localhost:5000/$image"
  docker push "localhost:5000/$image"
done

echo "done: $(date -Iseconds)" >&2
