#!/bin/bash
# Double-click to turn tcg_price_checker.py into "TCG Price Checker.app" (and a .zip of it to share).
# Needs Python 3.10+ from python.org. Takes a few minutes; nothing is installed into your own Python.
cd "$(dirname "$0")" || exit 1
PY=""
for p in python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
  if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys, tkinter; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then PY="$p"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3.10 or newer (with Tk) wasn't found. Install it from https://www.python.org/downloads/ and try again."
  read -r -p "Press Return to close." _; exit 1
fi
echo "Using $("$PY" --version) at $(command -v "$PY")"
if "$PY" tcg_price_checker.py --build-app; then
  rm -f "TCG Price Checker.app.zip"
  ditto -c -k --keepParent "TCG Price Checker.app" "TCG Price Checker.app.zip"
  echo
  echo "Built: TCG Price Checker.app (and TCG Price Checker.app.zip to send to another Mac)."
  echo "It only runs on Macs with the same chip type as this one ($(uname -m))."
  open -R "TCG Price Checker.app"
else
  echo
  echo "The build failed; the messages above say why."
fi
read -r -p "Press Return to close." _
