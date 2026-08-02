#!/bin/sh
# POSIX compatibility wrapper; install_agents.py is the canonical implementation.

set -eu

script_dir=$(CDPATH= cd "$(dirname "$0")" && pwd) || exit 1
python_executable=${PYTHON:-python3}

exec "$python_executable" "$script_dir/install_agents.py" "$@"
