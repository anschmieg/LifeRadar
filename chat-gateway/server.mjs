import express from 'express';
import pino from 'pino';

import { GatewayDb } from './src/db.mjs';
import { TelegramConnector } from './src/providers/telegram.mjs';
import { WhatsAppConnector } from './src/providers/whatsapp.mjs';

const logger = pino({ name: 'life-radar-chat-gateway' });
const app = express();
app.use(express.json({ limit: '1mb' }));

const db = new GatewayDb({ logger });
const connectors = new Map();

function boolEnv(name, fallback = false) {
  const value = (process.env[name] ?? String(fallback)).toLowerCase();
  return ['1', 'true', 'yes', 'on'].includes(value);
}

function sessionDir() {
  return process.env.LIFE_RADAR_CONNECTOR_SESSION_DIR || '/app/connectors';
}

function registerConnectors() {
  if (boolEnv('LIFE_RADAR_TELEGRAM_ENABLED', true)) {
    connectors.set('telegram', new TelegramConnector({
      db,
      logger,
      provider: 'telegram',
      sessionDir: `${sessionDir()}/telegram`,
    }));
  }

  if (boolEnv('LIFE_RADAR_WHATSAPP_ENABLED', true)) {
    connectors.set('whatsapp', new WhatsAppConnector({
      db,
      logger,
      provider: 'whatsapp',
      sessionDir: `${sessionDir()}/whatsapp`,
      unofficialAllowed: boolEnv('LIFE_RADAR_WHATSAPP_UNOFFICIAL_ALLOWED', true),
    }));
  }
}

function getConnector(provider) {
  const connector = connectors.get(provider);
  if (!connector) {
    const error = new Error(`Connector '${provider}' is not enabled`);
    error.statusCode = 404;
    throw error;
  }
  return connector;
}

function normalizeError(error) {
  return {
    error: error.message || String(error),
    code: error.code || 'gateway_error',
  };
}

registerConnectors();

app.get('/health', async (_req, res) => {
  res.json({
    status: 'ok',
    connectors: Array.from(connectors.keys()),
  });
});

app.get('/internal/connectors', async (_req, res) => {
  try {
    const statuses = await Promise.all(
      Array.from(connectors.values()).map((connector) => connector.getStatus())
    );
    res.json(statuses);
  } catch (error) {
    logger.error({ err: error }, 'failed to list connector status');
    res.status(500).json(normalizeError(error));
  }
});

app.post('/internal/connectors/:provider/login', async (req, res) => {
  try {
    const connector = getConnector(req.params.provider);
    const result = await connector.beginLogin(req.body ?? {});
    res.json(result);
  } catch (error) {
    logger.warn({ err: error, provider: req.params.provider }, 'login begin failed');
    res.status(error.statusCode || 500).json(normalizeError(error));
  }
});

app.get('/internal/connectors/:provider/login/:attemptId', async (req, res) => {
  try {
    const connector = getConnector(req.params.provider);
    const result = await connector.getLoginAttempt(req.params.attemptId);
    res.json(result);
  } catch (error) {
    logger.warn({ err: error, provider: req.params.provider }, 'login status failed');
    res.status(error.statusCode || 500).json(normalizeError(error));
  }
});

app.post('/internal/connectors/:provider/login/:attemptId/submit', async (req, res) => {
  try {
    const connector = getConnector(req.params.provider);
    const result = await connector.submitLoginStep(req.params.attemptId, req.body ?? {});
    res.json(result);
  } catch (error) {
    logger.warn({ err: error, provider: req.params.provider }, 'login step failed');
    res.status(error.statusCode || 500).json(normalizeError(error));
  }
});

app.post('/internal/connectors/:provider/logout', async (req, res) => {
  try {
    const connector = getConnector(req.params.provider);
    const result = await connector.logout(req.body ?? {});
    res.json(result);
  } catch (error) {
    logger.warn({ err: error, provider: req.params.provider }, 'logout failed');
    res.status(error.statusCode || 500).json(normalizeError(error));
  }
});

app.post('/internal/send', async (req, res) => {
  const { provider, external_id: externalId, content_text: contentText, conversation_id: conversationId } = req.body ?? {};
  try {
    if (!provider || !externalId || !contentText) {
      const error = new Error('provider, external_id, and content_text are required');
      error.statusCode = 400;
      throw error;
    }
    const connector = getConnector(provider);
    const result = await connector.sendMessage({
      externalId,
      contentText,
      conversationId: conversationId || null,
    });
    res.json(result);
  } catch (error) {
    logger.warn({ err: error, provider }, 'send failed');
    res.status(error.statusCode || 500).json(normalizeError(error));
  }
});

const port = Number.parseInt(process.env.LIFE_RADAR_CHAT_GATEWAY_PORT || '8020', 10);
app.listen(port, () => {
  logger.info({ port, connectors: Array.from(connectors.keys()) }, 'chat gateway listening');
});
