#!/bin/zsh
# Double-click to regenerate your Sick Jams mixes in Music.app.
cd "${0:A:h}"
/usr/bin/env python3 -m sick_jams "$@"
echo
read -k1 "?Press any key to close..."
