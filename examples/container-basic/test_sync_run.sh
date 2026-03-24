#!/usr/bin/env bash
# Test sync + run workflow with PYTHONPATH=/work/src

set -e

HOST=${1:-"jez21005@hpc2.storrs.hpc.uconn.edu"}
REMOTE_DIR='~/experiments/container-basic-test'
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

echo "=== Syncing to $HOST ==="

# Sync code (fast)
echo "--- Syncing code (dag.py, config.py, src/) ---"
time tar czf - -C "$SCRIPT_DIR" dag.py config.py src/ | ssh "$HOST" "rm -rf $REMOTE_DIR && mkdir -p $REMOTE_DIR && tar xzf - -C $REMOTE_DIR"

# Sync container (only if changed - compare checksums)
echo
echo "--- Syncing container (if changed) ---"
ssh "$HOST" "mkdir -p $REMOTE_DIR/.jernerics"

LOCAL_HASH=$(shasum -a 256 "$SCRIPT_DIR/.jernerics/container.tar.gz" | cut -d' ' -f1)
REMOTE_HASH=$(ssh "$HOST" "shasum -a 256 $REMOTE_DIR/.jernerics/container.tar.gz 2>/dev/null | cut -d' ' -f1" || echo "")

if [ "$LOCAL_HASH" = "$REMOTE_HASH" ]; then
    echo "Container unchanged, skipping sync"
else
    echo "Container changed, syncing..."
    time rsync -zL --progress "$SCRIPT_DIR/.jernerics/container.tar.gz" "$HOST:$REMOTE_DIR/.jernerics/"
fi

echo
echo "=== Testing PYTHONPATH setup ==="
ssh "$HOST" << 'ENDSSH'
cd ~/experiments/container-basic-test
gunzip -c .jernerics/container.tar.gz > /tmp/container-test.tar
apptainer exec --bind "$PWD:/work" docker-archive:///tmp/container-test.tar python -c '
import sys
print("PYTHONPATH entry:", [p for p in sys.path if "work/src" in p])
from container_basic import save_json
print("Import from mounted src/ OK")
'
rm -f /tmp/container-test.tar
ENDSSH
