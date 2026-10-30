#!/bin/sh
set -eu
PILOT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec /usr/bin/env python3 "$PILOT_ROOT/open_pilot.py"
