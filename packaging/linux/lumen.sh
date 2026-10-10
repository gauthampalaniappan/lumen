#!/bin/sh
# /usr/bin/lumen from the RPM and .deb packages.
# The app lives in /usr/share/lumen rather than site-packages so its module
# name cannot clash with any other Python package called "lumen".
export GTK_A11Y="${GTK_A11Y:-none}"
export PYTHONPATH="/usr/share/lumen${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 -m lumen "$@"
