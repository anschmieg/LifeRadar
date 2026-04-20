#!/bin/bash
# Microsoft Graph OAuth flow - for Outlook/Microsoft365 mailbox access
# Usage: ./msgraph-auth.sh [AUTH_CODE]

set -e

source .env

CLIENT_ID="${MSGRAPH_CLIENT_ID}"
CLIENT_SECRET="${MSGRAPH_CLIENT_SECRET}"
TENANT_ID="${MSGRAPH_TENANT_ID}"
REDIRECT_URI="http://localhost"
AUTHORITY="common"
SCOPE="offline_access https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/User.Read profile email openid"

AUTH_URL="https://login.microsoftonline.com/${AUTHORITY}/oauth2/v2.0/authorize?\
client_id=${CLIENT_ID}&\
redirect_uri=${REDIRECT_URI}&\
response_type=code&\
scope=${SCOPE}&\
response_mode=query&\
access_type=offline&\
prompt=consent"

echo "=============================================="
echo "Microsoft Graph OAuth Authorization"
echo "=============================================="
echo ""

if [[ -z "${1:-}" ]]; then
  echo "Open this URL in your browser:"
  echo ""
  echo "$AUTH_URL"
  echo ""
  echo "1. Sign in with your Microsoft/Outlook account"
  echo "2. Grant access to Mail and Profile"
  echo "3. The page will redirect to localhost - copy the 'code' parameter from the URL"
  echo ""
  echo "Then re-run with: $0 <AUTH_CODE>"
  echo ""
  echo "Example: $0 M.R3_BAY.C..."
  exit 0
fi

AUTH_CODE="$1"

echo "Exchanging authorization code for tokens..."

TOKEN_RESPONSE=$(xh POST "https://login.microsoftonline.com/${AUTHORITY}/oauth2/v2.0/token" \
  --form "client_id=${CLIENT_ID}" \
  --form "client_secret=${MSGRAPH_CLIENT_SECRET}" \
  --form "code=${AUTH_CODE}" \
  --form "grant_type=authorization_code" \
  --form "redirect_uri=${REDIRECT_URI}" \
  --ignore-stdin 2>&1)

if echo "$TOKEN_RESPONSE" | python3 -c "import sys,json; json.load(sys.stdin); print('valid')" 2>/dev/null; then
  REFRESH_TOKEN=$(echo "$TOKEN_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['refresh_token'])")
  ACCESS_TOKEN=$(echo "$TOKEN_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
  
  echo ""
  echo "=============================================="
  echo "Tokens obtained successfully!"
  echo "=============================================="
  
  # Update .env with new refresh token
  sed -i '' "s|MSGRAPH_REFRESH_TOKEN=.*|MSGRAPH_REFRESH_TOKEN=${REFRESH_TOKEN}|" .env
  echo "Updated MSGRAPH_REFRESH_TOKEN in .env"
  
  # Also update MSGRAPH_TENANT_ID to 'common' for future refreshes
  sed -i '' "s|MSGRAPH_TENANT_ID=.*|MSGRAPH_TENANT_ID=common|" .env
  echo "Updated MSGRAPH_TENANT_ID to 'common' in .env"
  
  # Test the token
  echo ""
  echo "Testing token..."
  TEST_RESULT=$(xh get "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox" \
    "Authorization: Bearer ${ACCESS_TOKEN}" 2>&1)
  
  if echo "$TEST_RESULT" | python3 -c "import sys,json; json.load(sys.stdin); print('valid')" 2>/dev/null; then
    echo "Token works! Connected to Microsoft Graph Mail"
  else
    echo "Response: $TEST_RESULT"
  fi
  
  echo ""
  echo "Done! Microsoft Graph integration ready."
else
  echo "Failed to obtain tokens"
  echo "$TOKEN_RESPONSE"
  exit 1
fi