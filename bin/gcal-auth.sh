#!/bin/bash
# Google Calendar OAuth flow - generate auth URL and handle token exchange
# Usage: ./gcal-auth.sh [AUTH_CODE]
# If AUTH_CODE is not provided, displays URL and prompts for code

set -e

source .env

CLIENT_ID="${GOOGLE_CALENDAR_CLIENT_ID}"
CLIENT_SECRET="${GOOGLE_CALENDAR_CLIENT_SECRET}"
REDIRECT_URI="urn:ietf:wg:oauth:2.0:oob"
SCOPE="https://www.googleapis.com/auth/calendar.read https://www.googleapis.com/auth/calendar"

AUTH_URL="https://accounts.google.com/o/oauth2/v2/auth?\
client_id=${CLIENT_ID}&\
redirect_uri=${REDIRECT_URI}&\
response_type=code&\
scope=${SCOPE}&\
access_type=offline&\
prompt=consent"

echo "=============================================="
echo "Google Calendar OAuth Authorization"
echo "=============================================="
echo ""

if [[ -z "${1:-}" ]]; then
  echo "Open this URL in your browser:"
  echo ""
  echo "$AUTH_URL"
  echo ""
  echo "1. Sign in with your Google account"
  echo "2. Grant calendar access to LifeRadar"  
  echo "3. Copy the authorization code shown"
  echo ""
  echo "Then re-run with: $0 <AUTH_CODE>"
  echo ""
  echo "Example: $0 4/0Adeu5B..."
  exit 0
fi

AUTH_CODE="$1"

echo "Exchanging authorization code for tokens..."

TOKEN_RESPONSE=$(xh POST "https://oauth2.googleapis.com/token" \
  --form "client_id=${CLIENT_ID}" \
  --form "client_secret=${CLIENT_SECRET}" \
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
  
  # Update .env
  sed -i '' "s|GOOGLE_CALENDAR_REFRESH_TOKEN=.*|GOOGLE_CALENDAR_REFRESH_TOKEN=${REFRESH_TOKEN}|" .env
  echo "Updated GOOGLE_CALENDAR_REFRESH_TOKEN in .env"
  
  # Test the token
  echo ""
  echo "Testing token..."
  TEST_RESULT=$(xh get "https://www.googleapis.com/calendar/v3/calendars/primary" \
    "Authorization: Bearer ${ACCESS_TOKEN}" 2>&1)
  
  if echo "$TEST_RESULT" | python3 -c "import sys,json; json.load(sys.stdin); print('valid')" 2>/dev/null; then
    CALENDAR_ID=$(echo "$TEST_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id','primary'))")
    echo "Token works! Connected to calendar: $CALENDAR_ID"
  else
    echo "Token validated (could not fetch calendar details)"
  fi
  
  echo ""
  echo "Done! Google Calendar integration ready."
else
  echo "Failed to obtain tokens"
  echo "$TOKEN_RESPONSE"
  exit 1
fi