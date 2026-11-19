#!/usr/bin/env bash
# Build one offline bundle for an air-gapped install: every container image, the Helm chart and a
# checksum manifest. Run this on a machine WITH internet access, carry the tarball in, then load it.
#
#   scripts/airgap-bundle.sh [--out DIR] [--nebulens-image IMAGE] [--with-operator] [--platform P]
#
# Options:
#   --out DIR           output directory (default: ./airgap)
#   --nebulens-image I    the NebuLens image to include (default: nebulens:0.1.0, built locally)
#   --with-operator     also bundle the OpenTelemetry Operator and auto-instrumentation agents
#   --platform P        image platform (default: linux/amd64)
set -euo pipefail

OUT=./airgap; NEBULENS_IMAGE=nebulens:0.1.0; WITH_OPERATOR=0; PLATFORM=linux/amd64
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="${2:?}"; shift 2 ;;
    --nebulens-image) NEBULENS_IMAGE="${2:?}"; shift 2 ;;
    --with-operator) WITH_OPERATOR=1; shift ;;
    --platform) PLATFORM="${2:?}"; shift 2 ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
HERE="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$OUT/images"

# Pin every digest in the bundle so the air-gapped side can prove it got what was built.
IMAGES=("$NEBULENS_IMAGE" "otel/opentelemetry-collector-contrib:0.111.0")
if [ "$WITH_OPERATOR" -eq 1 ]; then
  IMAGES+=(
    "ghcr.io/open-telemetry/opentelemetry-operator/opentelemetry-operator:0.162.0"
    "ghcr.io/open-telemetry/opentelemetry-collector-releases/opentelemetry-collector-k8s:0.162.0"
    "ghcr.io/open-telemetry/opentelemetry-operator/autoinstrumentation-java:latest"
    "ghcr.io/open-telemetry/opentelemetry-operator/autoinstrumentation-dotnet:latest"
    "ghcr.io/open-telemetry/opentelemetry-operator/autoinstrumentation-python:latest"
    "ghcr.io/open-telemetry/opentelemetry-operator/autoinstrumentation-nodejs:latest"
  )
fi

echo "Pulling and saving ${#IMAGES[@]} images for $PLATFORM ..."
: > "$OUT/images.txt"
for img in "${IMAGES[@]}"; do
  # A locally built image (no registry prefix) is used as-is instead of pulled.
  if ! docker image inspect "$img" >/dev/null 2>&1; then
    docker pull --platform "$PLATFORM" "$img"
  fi
  file="$OUT/images/$(echo "$img" | tr '/:' '__').tar"
  docker save "$img" -o "$file"
  echo "$img" >> "$OUT/images.txt"
  echo "  saved $img"
done

cp -r "$HERE/helm" "$OUT/helm"
cp "$HERE/ON-PREM.md" "$HERE/README.md" "$OUT/" 2>/dev/null || true

cat > "$OUT/load.sh" <<'LOAD'
#!/usr/bin/env bash
# Run INSIDE the air-gapped network. Loads every image and pushes it to your internal registry.
#   ./load.sh <internal-registry-host>[:port][/project]
set -euo pipefail
REG="${1:?usage: ./load.sh <internal-registry>}"
cd "$(dirname "$0")"
sha256sum -c checksums.txt
while read -r img; do
  [ -n "$img" ] || continue
  f="images/$(echo "$img" | tr '/:' '__').tar"
  docker load -i "$f"
  dest="$REG/$(echo "$img" | sed 's#^.*/##')"      # keep only name:tag under your registry
  docker tag "$img" "$dest"
  docker push "$dest"
  echo "  pushed $dest"
done < images.txt
echo
echo "Now install, pointing the chart at your registry:"
echo "  helm install nebulens ./helm/nebulens -n observability --create-namespace \\"
echo "    --set image.repository=$REG/nebulens --set collector.image=$REG/opentelemetry-collector-contrib:0.111.0 \\"
echo "    --set auth.token=<CHOOSE_A_TOKEN>"
LOAD
chmod +x "$OUT/load.sh"

( cd "$OUT" && find . -type f ! -name checksums.txt -print0 | xargs -0 sha256sum > checksums.txt )
TAR="nebulens-airgap-$(date +%Y%m%d).tar.gz"
tar -czf "$TAR" -C "$(dirname "$OUT")" "$(basename "$OUT")"
echo
echo "Bundle: $TAR  ($(du -h "$TAR" | cut -f1))"
echo "Carry it in, extract it, then run ./load.sh <internal-registry>"
