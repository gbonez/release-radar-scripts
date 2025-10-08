#!/bin/bash
# Run your Python script with secrets loaded from secrets.json

SECRETS_FILE="secrets.json"

if [ ! -f "$SECRETS_FILE" ]; then
  echo "❌ $SECRETS_FILE not found!"
  exit 1
fi

# Export each key/value as environment variable
echo "🔐 Loading secrets from $SECRETS_FILE..."
while IFS="=" read -r key value; do
  export "$key"="$value"
done < <(python3 -c "
import json
with open('$SECRETS_FILE') as f:
    data = json.load(f)
for k, v in data.items():
    print(f'{k}={v}')
")

# Confirm a few keys loaded (optional)
echo "✅ Environment variables loaded."
echo "Running Python script..."
echo

# Run your Python script (replace with your filename)
python3 script.py
