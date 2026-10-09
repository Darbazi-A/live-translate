#!/bin/sh
# macOS / Linux: run with  sh start.sh
# First run creates a private Python environment (.venv) and installs edge-tts into it,
# so nothing is installed system-wide (newer Linux distributions forbid that anyway).
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo
  echo "  Python 3 ვერ მოიძებნა. დააყენე Python 3.10 ან უფრო ახალი (იხ. README.md)."
  echo
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  if ! python3 -m venv .venv; then
    echo
    echo "  ვირტუალური გარემო ვერ შეიქმნა. Ubuntu/Debian-ზე გაუშვი: sudo apt install python3-venv"
    echo
    rm -rf .venv
    exit 1
  fi
fi

if ! .venv/bin/python -m pip install --quiet --disable-pip-version-check -r requirements.txt; then
  echo
  echo "  საჭირო პაკეტის (edge-tts) დაყენება ვერ მოხერხდა. შეამოწმე ინტერნეტი და სცადე თავიდან."
  echo
  exit 1
fi

exec .venv/bin/python server.py
