#!/bin/bash

# Load environment from .env
export $(grep -v '^#' .env | xargs)

echo "=== Refreshing MSGraph Token ==="
MSGRAPH_RESPONSE=$(xh POST "https://login.microsoftonline.com/${MSGRAPH_TENANT_ID}/oauth2/v2.0/token" \
  --form "client_id=${MSGRAPH_CLIENT_ID}" \
  --form "client_secret=${MSGRAPH_CLIENT_SECRET}" \
  --form "grant_type=refresh_token" \
  --form "refresh_token=${MSGRAPH_REFRESH_TOKEN}" \
  --form "scope=offline_access https://graph.microsoft.com/.default" \
  --ignore-stdin 2>&1)

if echo "$MSGRAPH_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print('valid')" 2>/dev/null; then
  NEW_MSGRAPH_TOKEN=$(echo "$MSGRAPH_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['refresh_token'])")
  sed -i '' "s|MSGRAPH_REFRESH_TOKEN=.*|MSGRAPH_REFRESH_TOKEN=${NEW_MSGRAPH_TOKEN}|" .env
  echo "MSGraph token refreshed successfully"
else
  echo "MSGraph refresh failed - may need re-authentication"
  echo "$MSGRAPH_RESPONSE" | head -5
fi

echo ""
echo "=== Refreshing Google Calendar Token ==="
GOOGLE_RESPONSE=$(xh POST "https://oauth2.googleapis.com/token" \
  --form "client_id=${GOOGLE_CALENDAR_CLIENT_ID}" \
  --form "client_secret=${GOOGLE_CALENDAR_CLIENT_SECRET}" \
  --form "grant_type=refresh_token" \
  --form "refresh_token=${GOOGLE_CALENDAR_REFRESH_TOKEN}" \
  --ignore-stdin 2>&1)

if echo "$GOOGLE_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print('valid')" 2>/dev/null; then
  NEW_GOOGLE_TOKEN=$(echo "$GOOGLE_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['refresh_token'])")
  sed -i '' "s|GOOGLE_CALENDAR_REFRESH_TOKEN=.*|GOOGLE_CALENDAR_REFRESH_TOKEN=${NEW_GOOGLE_TOKEN}|" .env
  echo "Google token refreshed successfully"
else
  echo "Google refresh failed - token may be expired/revoked, needs re-authentication"
  echo "$GOOGLE_RESPONSE" | head -5
fi

echo ""
echo "=== Tokens updated ==="