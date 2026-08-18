#!/usr/bin/env bash
# Download the Kokoro ONNX weights.
#
# Not vendored and not fetched automatically: these are ~340 MB, and a build
# step that silently downloads a third of a gigabyte is a bad neighbour. Run
# this once, then point tts.kokoro.model_path / voices_path at the results.
#
# Weights come from the kokoro-onnx project's release assets. Kokoro-82M is
# Apache-2.0 licensed.
set -euo pipefail

RELEASE="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
DEST="${1:-var/models}"

mkdir -p "$DEST"

fetch() {
  local name="$1"
  if [[ -f "$DEST/$name" ]]; then
    echo "already present: $DEST/$name"
    return
  fi
  echo "downloading $name ..."
  curl --fail --location --progress-bar --output "$DEST/$name.partial" "$RELEASE/$name"
  # Rename only on success, so an interrupted download is never mistaken for a
  # complete one on the next run.
  mv "$DEST/$name.partial" "$DEST/$name"
}

fetch "kokoro-v1.0.onnx"
fetch "voices-v1.0.bin"

echo
echo "Done. Set in config/pipeline.yaml:"
echo "  tts.kokoro.model_path:  $DEST/kokoro-v1.0.onnx"
echo "  tts.kokoro.voices_path: $DEST/voices-v1.0.bin"
