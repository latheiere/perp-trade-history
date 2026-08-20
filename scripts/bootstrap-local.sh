#!/usr/bin/env bash
set -euo pipefail

python_command="${1:-python3}"
venv_path="${2:-.venv}"

if [[ ! -x "${venv_path}/bin/python" ]]; then
  "${python_command}" -m venv "${venv_path}"
fi

"${venv_path}/bin/python" -m pip install -e ".[dashboard]"

if [[ ! -e config/config.toml ]]; then
  cp config/config.example.toml config/config.toml
fi

if [[ ! -e config/credentials.env ]]; then
  cp config/credentials.env.example config/credentials.env
fi

chmod 600 config/credentials.env

printf '%s\n' \
  "Bootstrap complete." \
  "1. Enable the required adapters in config/config.toml." \
  "2. Add read-only credentials to config/credentials.env." \
  "3. Run 'make first-run' to load available history and open the dashboard."
