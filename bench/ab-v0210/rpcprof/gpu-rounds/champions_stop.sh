#!/usr/bin/env bash
# champions_stop.sh — stop the serving stack by delegating to the box's own stop-all.
#
# It exists so that no CALLER ever has to carry a swept pattern in its own command
# line: stop-all.sh pgrep -f's 'launch-prod-|launch-trial-|strata --serve|
# strata-expert-helper|serve/server[.]py --engine strata', and a parent that
# inlined one of those strings would terminate itself. This file's name matches
# none of them, so it survives the sweep it triggers.
#
# Prints stop-all's own verdict and exits with its status. Does not touch sockets:
# the champions panel unlinks only the two per-arm socket paths it owns.
set -uo pipefail
exec bash /mnt/srcds/Strata/stop-all.sh
