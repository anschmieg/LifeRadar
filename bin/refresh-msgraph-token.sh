#!/bin/bash
# Refresh MSGraph token - works with common authority

set -e

source .env

CLIENT_ID="${MSGRAPH_CLIENT_ID}"
CLIENT_SECRET="${MSGRAPH_CLIENT_SECRET}"
TENANT_ID="${MSGRAPH_TENANT_ID:-common}"
AUTHORITY="${TENANT_ID}"
SCOPE="offline_access https://graph.microsoft.com/.default"

echo "=== Refreshing MSGraph Token ==="

TOKEN_RESP=$(curl -s -X POST "https://login.microsoftonline.com/${AUTHORITY}/oauth2/v2.0/token" \
  -d "client_id=${CLIENT_ID}" \
  -d "client_secret=${CLIENT_SECRET}" \
  -d "grant_type=refresh_token" \
  -d "refresh_token=${MSGRAPH_REFRESH_TOKEN}" \
  -d "scope=${SCOPE}" 2>&1)

if echo "$TOKEN_RESP" | python3 -c "import sys,json; json.load(sys.stdin); print('ok')" 2>/dev/null; then
  NEW_TOKEN=$(echo "$TOKEN_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['refresh_token'])")
  sed -i '' "s|MSGRAPH_REFRESH_TOKEN=.*|MSGRAPH_REFRESH_TOKEN=${NEW_TOKEN}|" .env
  echo "Token refreshed successfully"
  
  # Test with /me endpoint
  ACCESS_TOKEN=$(echo "$TOKEN_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
  TEST=$(curl -s "https://graph.microsoft.com/v1.0/me" -H "Authorization: Bearer ${ACCESS_TOKEN}" 2>&1)
  if echo "$TEST" | python3 -c "import sys,json; json.load(sys.stdin); print('ok')" 2>/dev/null; then
    echo "Token valid for Microsoft Graph"
  else
    echo "Token test returned: $TEST" | head -3
  fi
else
  echo "Refresh failed: $TOKEN_RESP" | head -5
  exit 1
fi