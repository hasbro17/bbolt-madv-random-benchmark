#!/usr/bin/env bash
# PLAN P5 on VM A (and the gate on B/C): run S3a once per setting and print a summary.
#
# Usage: calibrate.sh LABEL VARIANT CAP       CAP in bytes, or "none" for no cap
#        calibrate.sh --summary                table of every calibration run so far
# Results go to $RESULTS_ROOT/_calibration/<LABEL>/<VARIANT>-<n>/ and never reach the report.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/lib.sh"
load_runtime_env
croot="$RESULTS_ROOT/_calibration"

if [[ ${1:-} == --summary ]]; then
  printf '%-28s %-10s %12s %10s %12s %14s %8s %s\n' label variant cap took_s majflt read_mib oom status
  for rd in "$croot"/*/*/; do
    [[ -f $rd/run.json ]] || continue
    python3 - "$rd" "$HERE" <<'PY'
import json, os, sys
rd, here = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.join(here, "report"))
import aggregate
rj = json.load(open(os.path.join(rd, "run.json")))
try:
    _, m = aggregate.run_metrics(rd, "S3a")
except Exception as e:  # report and keep going
    m = {}
label = os.path.basename(os.path.dirname(rd.rstrip("/")))
print(f"{label:<28} {os.path.basename(rd.rstrip('/')):<10} {str(rj.get('cap')):>12} "
      f"{m.get('compaction_s', float('nan')):>10.2f} {m.get('compaction_majflt', float('nan')):>12.0f} "
      f"{m.get('compaction_read_mib', float('nan')):>14.1f} {str(rj.get('oom_kill', 0)):>8} {rj.get('status')} {rj.get('reason', '')}")
PY
  done
  exit 0
fi

label=${1:?label} variant=${2:?variant} cap=${3:?cap}
n=1
while [[ -e $croot/$label/$variant-$n ]]; do n=$((n + 1)); done
rd="$croot/$label/$variant-$n"
if [[ $cap == none ]]; then
  "$HERE/run-one.sh" S3a-ref "$variant" "$rd" || true
else
  CAP_OVERRIDE=$cap "$HERE/run-one.sh" S3a "$variant" "$rd" || true
fi
"$0" --summary | { head -1; grep -F "$label" | grep -F "$variant-$n" || true; }
