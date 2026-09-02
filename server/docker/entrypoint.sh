#!/bin/sh
set -e
UPSTREAM="${TORCHSERVE_UPSTREAM:-torchserve:8080}"
socat TCP-LISTEN:8080,fork,reuseaddr "TCP:${UPSTREAM}" &
exec "$@"
