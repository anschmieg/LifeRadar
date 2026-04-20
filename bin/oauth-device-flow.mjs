#!/usr/bin/env node
/**
 * Matrix Device Code OAuth flow for Matrix-compatible homeservers.
 *
 * Implements RFC 8628 Device Authorization Grant for Matrix SSO
 *
 * Usage:
 *   node oauth-device-flow.mjs [--homeserver <url>]
 *
 * Environment:
 *   MATRIX_HOMESERVER - Override homeserver URL
 */

import { parseArgs } from 'node:util';
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { createInterface } from 'node:readline';
import { spawn } from 'child_process';

const { values } = parseArgs({
  options: {
    homeserver: { type: 'string', short: 'h' },
    output: { type: 'string', short: 'o' },
    user: { type: 'string', short: 'u' },
    poll: { type: 'string', short: 'p' },
    help: { type: 'boolean', short: '?' },
  },
});

if (values.help) {
  console.log(`
Matrix Device Code OAuth Flow

Usage: node oauth-device-flow.mjs [options]

Options:
  --homeserver <url>   Matrix homeserver (or set LIFERADAR_MATRIX_HOMESERVER_URL)
  --user <user>        Username for token refresh testing
  --poll <code>        Poll existing device code instead of starting new flow
  --output <path>       Save tokens to file (default: ./matrix-session.json)
  --help               Show this help
`);
  process.exit(0);
}

const HOMESERVER = (
    process.env.LIFERADAR_MATRIX_HOMESERVER_URL
    || values.homeserver
    || ''
).replace(/\/$/, '');
const OUTPUT_PATH = values.output || './matrix-session.json';
const USER_ID = values.user;
const POLL_CODE = values.poll;

if (!HOMESERVER) {
  console.error('Missing Matrix homeserver. Set LIFERADAR_MATRIX_HOMESERVER_URL or pass --homeserver <url>.');
  process.exit(1);
}

// Device code flow endpoints
const DEVICE_CODE_URL = `${HOMESERVER}/_matrix/client/r0/device/new_device_code` ||
                        `${HOMESERVER}/_matrix/client/v1/device/new_device_code`;
const TOKEN_URL = `${HOMESERVER}/_matrix/client/r0/device/token` ||
                  `${HOMESERVER}/_matrix/client/v1/device/token`;

/**
 * Make HTTP request with proper JSON handling
 */
async function httpRequest(url, options = {}) {
  const response = await globalThis.fetch(url, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${data.error || response.statusText}`);
  }
  return data;
}

/**
 * Poll for token with exponential backoff
 */
async function pollForToken(deviceCode, interval = 5) {
  const startTime = Date.now();
  const timeout = 10 * 60 * 1000; // 10 minutes

  while (Date.now() - startTime < timeout) {
    try {
      const result = await httpRequest(TOKEN_URL, {
        method: 'POST',
        body: {
          grant_type: 'urn:ietf:params:oauth:grant-type:device_code',
          device_code: deviceCode,
        },
      });

      // Success - return token data
      return result;

    } catch (err) {
      if (err.message.includes('authorization_pending')) {
        // Still waiting
        process.stderr.write('.');
        await new Promise(r => setTimeout(r, interval * 1000));
        continue;
      }

      if (err.message.includes('slow_down')) {
        // Rate limited, increase interval
        interval = Math.min(interval * 2, 30);
        await new Promise(r => setTimeout(r, interval * 1000));
        continue;
      }

      throw err;
    }
  }

  throw new Error('OAuth flow timed out');
}

/**
 * Exchange existing refresh token for new access token
 */
async function refreshToken(refreshToken, userId, homeserver) {
  const response = await httpRequest(`${homeserver}/_matrix/client/r0/refresh`, {
    method: 'POST',
    body: {
      refresh_token: refreshToken,
    },
  });

  return response;
}

/**
 * Main OAuth flow
 */
async function main() {
  console.log('=== Matrix Device Code OAuth Flow ===');
  console.log(`Homeserver: ${HOMESERVER}`);

  // Check for existing session
  if (existsSync(OUTPUT_PATH)) {
    try {
      const existing = JSON.parse(readFileSync(OUTPUT_PATH, 'utf8'));
      if (existing.access_token && existing.user_id) {
        console.log(`Found existing session for ${existing.user_id}`);
        console.log(`Token expires: ${existing.expires_at || 'unknown'}`);
        console.log('');

        // Try to use existing token
        const response = await httpRequest(`${HOMESERVER}/_matrix/client/v3/account/whoami`, {
          headers: { Authorization: `Bearer ${existing.access_token}` },
        }).catch(() => null);

        if (response?.user_id) {
          console.log(`Token still valid for ${response.user_id}`);
          console.log('Use --poll to check pending auth or --user <id> to test refresh');
          return;
        }

        console.log('Token expired, need re-authentication');
      }
    } catch (e) {
      console.log('Existing session invalid, starting fresh auth');
    }
  }

  // Poll mode - check existing code
  if (POLL_CODE) {
    console.log(`Polling for code: ${POLL_CODE}`);
    const result = await pollForToken(POLL_CODE);
    console.log('\nAuthorization received!');
    saveSession(result, OUTPUT_PATH, HOMESERVER);
    return;
  }

  // Start new device code flow
  console.log('\nStarting device authorization flow...');

  const clientId = `liferadar-${Date.now()}`;

  // Request device code
  let deviceCode;
  try {
    const result = await httpRequest(DEVICE_CODE_URL, {
      method: 'POST',
      body: {
        scope: 'openid profile',
      },
    });
    deviceCode = result.device_code;
  } catch (err) {
    // Try v3 endpoint
    try {
      const result = await httpRequest(`${HOMESERVER}/_matrix/client/v3/device/new_device_code`, {
        method: 'POST',
        body: { scope: 'openid profile' },
      });
      deviceCode = result.device_code;
    } catch (err2) {
      throw new Error(`Failed to start auth: ${err2.message}`);
    }
  }

  // Get verification URL and user code
  const authUrl = `${HOMESERVER}/#/device?code=${deviceCode}`;
  console.log('\n=== Authorization Required ===');
  console.log('');
  console.log(`1. Open this URL in your browser:`);
  console.log(`   ${HOMESERVER}/_matrix/client/r0/device`);
  console.log('');
  console.log(`2. Enter this code: ${deviceCode}`);
  console.log('');
  console.log('Waiting for authorization...');
  console.log('(Press Ctrl+C to cancel)');

  try {
    const result = await pollForToken(deviceCode);
    console.log('\n\nAuthorization complete!');

    saveSession(result, OUTPUT_PATH, HOMESERVER);

  } catch (err) {
    console.error(`\n\nError: ${err.message}`);
    process.exit(1);
  }
}

/**
 * Save session to file
 */
function saveSession(tokenData, outputPath, homeserver) {
  const expiresIn = tokenData.expires_in;
  const expiresAt = expiresIn
    ? new Date(Date.now() + expiresIn * 1000).toISOString()
    : null;

  const session = {
    access_token: tokenData.access_token,
    refresh_token: tokenData.refresh_token || null,
    user_id: tokenData.user_id,
    device_id: tokenData.device_id || null,
    homeserver,
    expires_at: expiresAt,
    expires_in: expiresIn || null,
    saved_at: new Date().toISOString(),
  };

  writeFileSync(outputPath, JSON.stringify(session, null, 2));
  console.log(`Saved to: ${outputPath}`);
  console.log(`User: ${session.user_id}`);
  console.log(`Token: ${session.access_token.substring(0, 20)}...`);
  console.log(`Expires: ${session.expires_at || 'refresh-token based (no hard expiry'}`);
}

// Run
main().catch(err => {
  console.error('Error:', err.message);
  process.exit(1);
});
