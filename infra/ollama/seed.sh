#!/bin/sh
# Explicit one-shot registry download. No business data or application network.
set -eu
export OLLAMA_HOST=127.0.0.1:11434
ollama serve >/tmp/seed-server.log 2>&1 &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true; wait "$server_pid" 2>/dev/null || true' EXIT INT TERM
ready=0
for attempt in $(seq 1 60); do
    if ollama list >/dev/null 2>&1; then ready=1; break; fi
    sleep 1
done
test "$ready" = 1
manifest=/models/manifests/registry.ollama.ai/library/qwen3/1.7b
if [ ! -f "$manifest" ]; then ollama pull qwen3:1.7b; fi
stored_manifest_sha=0f45bae8cd2fec097cf09b400b078cf42388a1f5702f4058d2d6d641a09db46d # pragma: allowlist secret -- public model-manifest checksum
printf '%s  %s\n' "$stored_manifest_sha" \
    '/models/manifests/registry.ollama.ai/library/qwen3/1.7b' | sha256sum -c -
# Ollama adds the original registry manifest as a compatibility/provenance layer.
# Registry digest: 8f68893c685c3ddff2aa3fffce2aa60a30bb2da65ca488b61fff134a4d1730e7.
for blob in /models/blobs/sha256-*; do
    name=${blob##*/}
    printf '%s  %s\n' "${name#sha256-}" "$blob" | sha256sum -c - >/dev/null
done
printf '%s\n' 'qwen3:1.7b manifest verified' >/models/seed-complete.txt
