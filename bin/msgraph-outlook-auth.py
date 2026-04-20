#!/usr/bin/env python3
"""Microsoft Graph OAuth - Authorization Code flow for Outlook.com"""

import urllib.parse
import urllib.request
import json
import re
import webbrowser
import http.server
import threading
import time
import sys

# Load env
env = {}
with open('.env') as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            env[key] = value

CLIENT_ID = env.get('MSGRAPH_CLIENT_ID', '')
CLIENT_SECRET = env.get('MSGRAPH_CLIENT_SECRET', '')
# Use consumers endpoint for personal Microsoft accounts
AUTHORITY = 'consumers'
REDIRECT_URI = 'http://localhost:8765/callback'
SCOPE = urllib.parse.quote('offline_access https://graph.microsoft.com/Mail.Read')

auth_url = f'https://login.microsoftonline.com/{AUTHORITY}/oauth2/v2.0/authorize?client_id={CLIENT_ID}&redirect_uri={urllib.parse.quote(REDIRECT_URI)}&response_type=code&scope={SCOPE}&access_type=offline&prompt=consent'

print("==============================================")
print("Microsoft Graph OAuth - Outlook.com Account")
print("==============================================")
print()

code = None
error = None

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global code, error
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if 'code' in params:
            code = params['code'][0]
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<html><body><h1>Authentication successful! You can close this window.</h1></body></html>')
            print("Got code!")
        elif 'error' in params:
            error = params['error'][0]
            self.send_response(400)
            self.end_headers()
            self.wfile.write(f'<html><body><h1>Error: {error}</h1></body></html>'.encode())
    def log_message(self, format, *args):
        pass

print("Opening browser for authentication...")
webbrowser.open(auth_url)

print("Waiting for redirect...")
server = http.server.HTTPServer(('localhost', 8765), Handler)
thread = threading.Thread(target=server.handle_request)
thread.daemon = True
thread.start()

# Wait for code or timeout
start = time.time()
while code is None and error is None and time.time() - start < 120:
    time.sleep(0.5)

if error:
    print(f"Error: {error}")
    sys.exit(1)

if not code:
    print("Timeout waiting for auth code")
    sys.exit(1)

print(f"Got code: {code[:30]}...")
print()

# Exchange code for tokens
data = urllib.parse.urlencode({
    'client_id': CLIENT_ID,
    'client_secret': CLIENT_SECRET,
    'code': code,
    'grant_type': 'authorization_code',
    'redirect_uri': REDIRECT_URI
}).encode()

req = urllib.request.Request(
    f'https://login.microsoftonline.com/{AUTHORITY}/oauth2/v2.0/token',
    data=data,
    method='POST'
)

print("Exchanging code for tokens...")
with urllib.request.urlopen(req, timeout=30) as resp:
    token_data = json.loads(resp.read())

refresh_token = token_data['refresh_token']
access_token = token_data['access_token']

print("Tokens obtained!")
print()

# Update .env
with open('.env', 'r') as f:
    content = f.read()

content = re.sub(r'MSGRAPH_REFRESH_TOKEN=.*', f'MSGRAPH_REFRESH_TOKEN={refresh_token}', content)
content = re.sub(r'MSGRAPH_TENANT_ID=.*', 'MSGRAPH_TENANT_ID=consumers', content)

with open('.env', 'w') as f:
    f.write(content)

print("Updated .env with new tokens")
print()

# Test the token
req = urllib.request.Request(
    'https://graph.microsoft.com/v1.0/me/mailFolders/Inbox',
    headers={'Authorization': f'Bearer {access_token}'}
)

try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
        print(f"SUCCESS! Connected to Outlook.com")
        print(f"  Total emails: {result.get('totalItemCount', 'N/A')}")
        print(f"  Unread: {result.get('unreadItemCount', 'N/A')}")
except Exception as e:
    print(f"Token test: {e}")

print()
print("Done!")