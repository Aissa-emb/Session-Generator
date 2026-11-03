#!/bin/zsh
cd -- "${0:A:h}" || exit 1
python3 tools/studio.py start --open
if [[ $? -ne 0 ]]; then
  read -r '?Press Enter to close.'
fi
