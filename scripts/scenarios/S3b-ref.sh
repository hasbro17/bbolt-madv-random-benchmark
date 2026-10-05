# shellcheck shell=bash disable=SC2034
# S3b reference: same as S3b without the memory cap.
source "$(dirname "${BASH_SOURCE[0]}")/S3b.sh"
SCENARIO_CAPPED=0
