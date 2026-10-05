# shellcheck shell=bash disable=SC2034
# S3a reference: same as S3a without the memory cap (no reclaim expected).
source "$(dirname "${BASH_SOURCE[0]}")/S3a.sh"
SCENARIO_CAPPED=0
