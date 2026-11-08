#!/bin/zsh
cd -- "${0:A:h}" || exit 1
python3 local_server.py
printf '\nServer stopped. Press Enter to close.\n'
read
