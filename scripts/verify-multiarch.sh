#!/usr/bin/env bash
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
cd "$root"

run_id="$$"
api_container="ecc-multiarch-api-$run_id"
web_container="ecc-multiarch-web-$run_id"
images=()
networks=()

cleanup() {
  docker container rm --force "$api_container" "$web_container" >/dev/null 2>&1 || true
  if ((${#networks[@]} > 0)); then
    docker network rm "${networks[@]}" >/dev/null 2>&1 || true
  fi
  if ((${#images[@]} > 0)); then
    docker image rm --force "${images[@]}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

wait_for_url() {
  local url="$1"
  for _ in {1..30}; do
    if curl --fail --silent "$url" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "smoke test timed out: $url" >&2
  return 1
}

for platform in linux/arm64 linux/amd64; do
  architecture="${platform#linux/}"
  suffix="${architecture}-${run_id}"
  api_image="ecc-api-multiarch:$suffix"
  worker_image="ecc-worker-multiarch:$suffix"
  web_image="ecc-web-multiarch:$suffix"
  network="ecc-multiarch-$suffix"
  images+=("$api_image" "$worker_image" "$web_image")
  networks+=("$network")

  docker buildx build --platform "$platform" --load --tag "$api_image" --file apps/api/Dockerfile .
  docker buildx build --platform "$platform" --load --tag "$worker_image" --file apps/worker/Dockerfile .
  docker buildx build --platform "$platform" --load --tag "$web_image" --file apps/web/Dockerfile .

  for image in "$api_image" "$worker_image" "$web_image"; do
    actual="$(docker image inspect --format '{{.Architecture}}' "$image")"
    [[ "$actual" == "$architecture" ]] || {
      echo "$image: expected $architecture, got $actual" >&2
      exit 1
    }
  done

  docker network create "$network" >/dev/null
  docker run --detach --rm --platform "$platform" --name "$api_container" \
    --network "$network" --network-alias api \
    --publish "127.0.0.1::8000" "$api_image" >/dev/null
  api_port="$(docker port "$api_container" 8000/tcp | awk -F: '{print $NF}')"
  wait_for_url "http://127.0.0.1:$api_port/health/live"
  curl --fail --silent "http://127.0.0.1:$api_port/version" | grep -q '"version":"dev"'
  docker run --rm --platform "$platform" "$worker_image" \
    python -c 'from apps.worker.main import PollingWorker; assert PollingWorker'

  docker run --detach --rm --platform "$platform" --name "$web_container" \
    --network "$network" \
    --publish "127.0.0.1::80" "$web_image" >/dev/null
  web_port="$(docker port "$web_container" 80/tcp | awk -F: '{print $NF}')"
  wait_for_url "http://127.0.0.1:$web_port/"
  docker container rm --force "$api_container" "$web_container" >/dev/null
  docker network rm "$network" >/dev/null

  echo "$platform build and smoke tests passed"
done
