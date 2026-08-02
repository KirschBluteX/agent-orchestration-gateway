#!/bin/sh
# POSIX compatibility wrapper for the repository's canonical Python test command.

set -eu

script_dir=$(CDPATH= cd "$(dirname "$0")" && pwd) || exit 1
repo_root=$(CDPATH= cd "$script_dir/../../.." && pwd) || exit 1
python_executable=${PYTHON:-python3}

cd "$repo_root"
exec "$python_executable" -X utf8 -B -m unittest discover -s tests -v
