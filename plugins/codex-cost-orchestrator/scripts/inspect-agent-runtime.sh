#!/bin/sh
# POSIX compatibility wrapper; inspect_agent_runtime.py is canonical.

set -eu

script_dir=$(CDPATH= cd "$(dirname "$0")" && pwd) || exit 1
python_executable=${PYTHON:-python3}

exec "$python_executable" "$script_dir/inspect_agent_runtime.py" "$@"
