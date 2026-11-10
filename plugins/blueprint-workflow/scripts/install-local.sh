#!/usr/bin/env sh
# Use BLUEPRINT_WORKFLOW_DATA_DIR for a non-default private data location.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python_command=${BLUEPRINT_WORKFLOW_SETUP_PYTHON:-python3}
"$python_command" -c 'import sys; assert sys.version_info >= (3, 12), "Python 3.12+ is required"'
exec "$python_command" "$script_dir/launch.py" install "$@"
