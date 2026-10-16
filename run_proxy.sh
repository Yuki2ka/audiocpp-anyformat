#!/bin/sh
# Run the anyformat proxy in front of a running audiocpp_server.
# Adjust --backend if your server listens elsewhere.
exec python3 "$(dirname "$0")/proxy/audiocpp_anyformat_proxy.py" \
    --backend http://127.0.0.1:8081 "$@"
