#!/usr/bin/env python3
"""Microsoft Graph OAuth - Simple auth URL generator and code exchanger."""

import urllib.parse
import urllib.request
import json
import re
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
REDIRECT_URI = 'http://localhost:8765'

print("==============================================")
print("Microsoft Graph OAuth")
print("==============================================")
print()

if len(sys.argv) > 1:
    # Code provided - exchange for tokens
    code = sys.argv[1]
    print(f"Exchanging code for tokens...")
    
    data = urllib.parse.urlencode({
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'code': code,
        'grant_type': 'authorization_code',
        'redirect_uri': REDIRECT_URI
    }).encode()
    
    req = urllib.request.Request(
        'https://login.microsoftonline.com/common/oauth2/v2.0/token',
        data=data,
        method='POST'
    )
    
    try:
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
        content = re.sub(r'MSGRAPH_TENANT_ID=.*', 'MSGRAPH_TENANT_ID=common', content)
        
        with open('.env', 'w') as f:
            f.write(content)
        
        print("Updated .env")
        print()
        
        # Test token
        req = urllib.request.Request(
            'https://graph.microsoft.com/v1.0/me/mailFolders/Inbox',
            headers={'Authorization': f'Bearer {access_token}'}
        )
        
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                print(f"SUCCESS! Connected to Microsoft Graph Mail")
                print(f"  Total emails: {result.get('totalItemCount', 'N/A')}")
        except Exception as e:
            print(f"Token test result: {e}")
        
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
        
else:
    # Generate auth URL
    scope = urllib.parse.quote('offline_access https://graph.microsoft.com/Mail.Read')
    auth_url = f'https://login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id={CLIENT_ID}&redirect_uri={urllib.parse.quote(REDIRECT_URI)}&response_type=code&scope={scope}&access_type=offline&prompt=consent'
    
    print("Open this URL in your browser:")
    print()
    print(auth_url)
    print()
    print("Sign in with your Microsoft account, grant access,")
    print("then paste the full redirect URL here:")
    print()
    print("Example redirect URL:")
    print("http://localhost:8765?code=M.R3_BAY.C...")
    print()
    print("Or run with code directly:")
    print(f"  python3 bin/msgraph-auth.py <CODE>")
    print()
    
    redirect = input("Paste redirect URL or code: ").strip()
    
    if 'code=' in redirect:
        # Extract code from URL
        code = urllib.parse.parse_qs(urllib.parse.urlparse(redirect).query)['code'][0]
    else:
        code = redirect
    
    # Recurse with code
    import subprocess
    subprocess.run([sys.executable, sys.argv[0], code])