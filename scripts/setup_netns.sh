#!/bin/bash
# Create network namespaces for UE isolation
# Run once before starting any UEs

set -e

for ns in ue1 ue2 ue3; do
    if ! sudo ip netns list | grep -q "^${ns}"; then
        sudo ip netns add "$ns"
        echo "Created netns: $ns"
    else
        echo "Netns already exists: $ns"
    fi
done

echo "Network namespaces ready."