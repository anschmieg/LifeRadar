#!/bin/bash
# Microsoft Graph OAuth flow using device code
# Usage: ./msgraph-device-auth.sh

set -e

source .env

CLIENT_ID="${MSGRAPH_CLIENT_ID}"
CLIENT_SECRET="${MSGRAPH_CLIENT_SECRET}"
AUTHORITY="common"
SCOPE="offline_access%20https://graph.microsoft.com/Mail.Read"

echo "=============================================="
echo "Microsoft Graph OAuth - Device Code Flow"
echo "=============================================="
echo ""

# Get device code using curl (more reliable than xh for this)
DEVICE_RESP=$(curl -s -X POST "https://login.microsoftonline.com/${AUTHORITY}/oauth2/v2.0/devicecode" \
  -d "client_id=${CLIENT_ID}" \
  -d "scope=${SCOPE}" 2>&1)

# Extract values using python
USER_CODE=$(echo "$DEVICE_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('user_code', ''))")
VERIFICATION_URL=$(echo "$DEVICE_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('verification_url', ''))")
DEVICE_CODE=$(echo "$DEVICE_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('device_code', ''))")
INTERVAL=$(echo "$DEVICE_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('interval', 5))")
MESSAGE=$(echo "$DEVICE_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('message', 'Visit URL and enter code'))")

if [[ -z "$DEVICE_CODE" ]]; then
  echo "Failed to get device code"
  echo "$DEVICE_RESP"
  exit 1
fi

echo "User Code: $USER_CODE"
echo "URL: $VERIFICATION_URL"
echo ""
echo "$MESSAGE"
echo ""
echo "Waiting for authentication..."
echo "(Press Ctrl+C to cancel)"
echo ""

# Poll for token
while true; do
  sleep $INTERVAL
  
  TOKEN_RESP=$(curl -s -X POST "https://login.microsoftonline.com/${AUTHORITY}/oauth2/v2.0/token" \
    -d "client_id=${CLIENT_ID}" \
    -d "client_secret=${MSGRAPH_CLIENT_SECRET}" \
    -d "grant_type=urn:ietf:wg:oauth:2.0:device_code" \
    -d "code=${DEVICE_CODE}" 2>&1)
  
  ERROR=$(echo "$TOKEN_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('error', 'ok'))" 2>/dev/null)
  
  if [[ "$ERROR" == "authorization_pending" ]]; then
    echo -n "."
    continue
  elif [[ "$ERROR" != "ok" ]]; then
    echo ""
    echo "Error: $ERROR"
    echo "$TOKEN_RESP"
    exit 1
  else
    echo ""
    echo "Token obtained!"
    break
  fi
done

REFRESH_TOKEN=$(echo "$TOKEN_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['refresh_token'])")
ACCESS_TOKEN=$(echo "$TOKEN_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

echo ""
echo "=============================================="
echo "Tokens obtained successfully!"
echo "=============================================="

# Update .env
sed -i '' "s|MSGRAPH_REFRESH_TOKEN=.*|MSGRAPH_REFRESH_TOKEN=${REFRESH_TOKEN}|" .env
sed -i '' "s|MSGRAPH_TENANT_ID=.*|MSGRAPH_TENANT_ID=common|" .env
echo "Updated MSGRAPH_REFRESH_TOKEN and MSGRAPH_TENANT_ID in .env"

# Test the token
echo ""
echo "Testing token..."
TEST=$(curl -s "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" 2>&1)

if echo "$TEST" | python3 -c "import sys,json; json.load(sys.stdin); print('valid')" 2>/dev/null; then
  echo "Token works! Connected to Microsoft Graph Mail"
else
  echo "Response: $TEST" | head -5
fi

echo ""
echo "Done!"