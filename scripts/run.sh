#!/bin/sh
# Lançador portátil: usa o primeiro Python que funcionar (python3, python ou py).
# Sem Python, sai em silêncio com código 0 para não quebrar hooks nem a status line.
dir=$(dirname "$0")
for p in python3 python py; do
  if "$p" -c "import sys" >/dev/null 2>&1; then
    exec "$p" "$dir/agentmeter.py" "$@"
  fi
done
exit 0
