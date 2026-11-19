#!/usr/bin/env bash
# Turn NebuLens zero-code tracing on (or off) for workloads. No code or image changes: the
# OpenTelemetry Operator injects the language agent when the pod restarts.
#
#   scripts/instrument.sh <java|dotnet|python|nodejs|go> -n <namespace> <workload>... [options]
#
# Options:
#   --kind <deployment|statefulset|daemonset>   workload kind (default: deployment)
#   --musl                                      .NET on Alpine/musl images
#   --remove                                    switch tracing off again
#   --dry-run                                   print what would change
#
# Environment: NEBULENS_NS (namespace where NebuLens is installed, default observability),
#              NEBULENS_INSTRUMENTATION (Instrumentation name, default nebulens).
set -euo pipefail

PNS="${NEBULENS_NS:-observability}"
INAME="${NEBULENS_INSTRUMENTATION:-nebulens}"
KIND=deployment; NS=""; REMOVE=0; MUSL=0; DRY=0; LANG_=""; WORKLOADS=()

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-1}"; }

[ $# -ge 1 ] || usage
case "$1" in -h|--help) usage 0 ;; esac
LANG_="$1"; shift
case "$LANG_" in java|dotnet|python|nodejs|go) ;; *) echo "error: language must be java, dotnet, python, nodejs or go" >&2; usage ;; esac

while [ $# -gt 0 ]; do
  case "$1" in
    -n|--namespace) NS="${2:?missing namespace}"; shift 2 ;;
    --kind) KIND="${2:?missing kind}"; shift 2 ;;
    --musl) MUSL=1; shift ;;
    --remove) REMOVE=1; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage 0 ;;
    -*) echo "error: unknown option $1" >&2; usage ;;
    *) WORKLOADS+=("$1"); shift ;;
  esac
done
[ -n "$NS" ] || { echo "error: -n <namespace> is required" >&2; usage; }
[ ${#WORKLOADS[@]} -gt 0 ] || { echo "error: give at least one workload name" >&2; usage; }
case "$KIND" in deployment|statefulset|daemonset) ;; *) echo "error: --kind must be deployment, statefulset or daemonset" >&2; exit 1 ;; esac

# The Instrumentation resource must exist (Operator installed + NebuLens chart applied).
if ! kubectl get instrumentation "$INAME" -n "$PNS" >/dev/null 2>&1; then
  echo "error: Instrumentation '$INAME' not found in namespace '$PNS'." >&2
  echo "       Install the Operator (helm ... --set opentelemetry-operator.enabled=true --wait) and re-run helm upgrade." >&2
  exit 1
fi

KEY="instrumentation.opentelemetry.io/inject-${LANG_}"
RTKEY="instrumentation.opentelemetry.io/otel-dotnet-auto-runtime"
if [ "$REMOVE" -eq 1 ]; then
  VAL=null; RTVAL=null
else
  VAL="\"${PNS}/${INAME}\""; RTVAL='"linux-musl-x64"'
fi
ANN="\"${KEY}\":${VAL}"
if [ "$LANG_" = dotnet ] && [ "$MUSL" -eq 1 ]; then ANN="${ANN},\"${RTKEY}\":${RTVAL}"; fi
PATCH="{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{${ANN}}}}}}"

for w in "${WORKLOADS[@]}"; do
  if [ "$DRY" -eq 1 ]; then
    echo "[dry-run] kubectl patch ${KIND} ${w} -n ${NS} -p '${PATCH}'"
  else
    kubectl patch "${KIND}" "${w}" -n "${NS}" -p "${PATCH}"
  fi
done
[ "$DRY" -eq 1 ] || echo "Done. Pods restart now; traces appear in NebuLens within a minute."
