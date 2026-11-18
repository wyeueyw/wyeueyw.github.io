#!/bin/zsh
cd -- "${0:A:h}" || exit 1
for editor_python in "$HOME/.local/share/uv/python/cpython-3.12-macos-aarch64-none/bin/python3" /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if [[ -x "$editor_python" ]]; then
    exec "$editor_python" -B editor-server.py
  fi
done
print "Python 3 is required."
read -k 1
