#!/usr/bin/env bash
# The log lines age out of the evidence window on their own; revert the release.
. "$(dirname "$0")/../../lib.sh"

bash "$(dirname "$0")/../bad-config/revert.sh"
