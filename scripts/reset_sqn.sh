#!/bin/bash
# Reset SQN to 000000000000 for all UEs in user_db.csv
# Run this before starting the EPC if UEs fail to authenticate
# (MAC code failure / Authentication Failure)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB_FILE="$SCRIPT_DIR/../configs/user_db.csv"

if [ ! -f "$DB_FILE" ]; then
    echo "ERROR: user_db.csv not found at $DB_FILE"
    exit 1
fi

sed -i 's/,[0-9a-fA-F]\{12\},/,000000000000,/g' "$DB_FILE"

# Also update the copy srsEPC might read from
if [ -f /root/.config/srsran/user_db.csv ]; then
    sudo cp "$DB_FILE" /root/.config/srsran/user_db.csv
    echo "Reset SQN in $DB_FILE and /root/.config/srsran/user_db.csv"
else
    echo "Reset SQN in $DB_FILE"
fi