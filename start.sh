#!/bin/sh
set -eu
umask 077
exec python -u main.py
