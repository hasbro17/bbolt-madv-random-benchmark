# shellcheck shell=bash disable=SC2034
# S3a with MGLRU switched off (run-matrix.sh flips it around this scenario only).
source "$(dirname "${BASH_SOURCE[0]}")/S3a.sh"
SCENARIO_MGLRU=off
