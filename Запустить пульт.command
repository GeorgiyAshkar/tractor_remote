#!/bin/zsh
set -eu
cd -- "${0:A:h}"

python_cli=""
path_python="$(command -v python3 2>/dev/null || true)"
for python_candidate in "$path_python" "$HOME/.local/bin/python3" /usr/bin/python3; do
  if [[ -n "$python_candidate" && -x "$python_candidate" ]] && \
     "$python_candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    python_cli="$python_candidate"
    break
  fi
done
if [[ -z "$python_cli" ]]; then
  print -u2 'Нужен Python 3.9 или новее. Установите его и проверьте команду python3 в Терминале.'
  exit 1
fi

exec "$python_cli" -B web_control/server.py --open-browser "$@"
