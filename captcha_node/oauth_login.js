'use strict';

const fs = require('fs/promises');
const os = require('os');
const path = require('path');
const { chromium } = require('playwright-core');

const AUTHORIZE_URL = process.argv[2] || '';
const EXPECTED_STATE = process.argv[3] || '';
const CHROMIUM_PATH = process.env.ZCODE_CHROMIUM_PATH || '/usr/bin/chromium';
const TIMEOUT_MS = Number.parseInt(process.env.ZCODE_OAUTH_BROWSER_TIMEOUT || '600000', 10);
const HEADLESS = process.env.ZCODE_OAUTH_BROWSER_HEADLESS === '1';
const DIAG_MAX = 200;

function diag(tag, detail) {
  let text = '';
  if (detail instanceof Error) text = detail.message || detail.name || '';
  else if (typeof detail === 'string') text = detail;
  else if (detail != null) text = Object.prototype.toString.call(detail);
  text = String(text).replace(/\s+/g, ' ').slice(0, DIAG_MAX);
  process.stderr.write(`[oauth-browser] ${tag}${text ? `: ${text}` : ''}\n`);
}

function validateInputs() {
  if (!EXPECTED_STATE || EXPECTED_STATE.length < 16) throw new Error('invalid OAuth state');
  const url = new URL(AUTHORIZE_URL);
  if (url.protocol !== 'https:' || url.hostname !== 'chat.z.ai') {
    throw new Error('invalid authorize origin');
  }
  if (url.pathname !== '/api/oauth/authorize') throw new Error('invalid authorize path');
  if (url.searchParams.get('state') !== EXPECTED_STATE) throw new Error('authorize state mismatch');
}

function readCallbackUrl(value, expectedState = EXPECTED_STATE) {
  if (!value) return null;
  let url;
  try {
    url = new URL(value);
  } catch (_error) {
    return null;
  }
  if (url.protocol !== 'zcode:' || url.hostname !== 'oauth') return null;
  if (url.pathname.replace(/\/+$/, '') !== '/callback') return null;
  const code = url.searchParams.get('code') || url.searchParams.get('authCode');
  const state = url.searchParams.get('state');
  if (!code || state !== expectedState) return null;
  return code;
}

async function codeFromPage(page, expectedState = EXPECTED_STATE) {
  const current = page.url();
  try {
    const url = new URL(current);
    if (url.hostname === 'zcode.z.ai' && url.pathname === '/app/oauth/login') {
      const code = url.searchParams.get('code') || url.searchParams.get('authCode');
      const state = url.searchParams.get('state');
      if (code && state === expectedState) return code;
      if (url.searchParams.get('error')) throw new Error('authorization was rejected');
    }
  } catch (error) {
    if (error instanceof Error && error.message === 'authorization was rejected') throw error;
  }

  const href = await page
    .locator('a[href^="zcode://oauth/callback"]')
    .first()
    .getAttribute('href')
    .catch(() => null);
  return readCallbackUrl(href, expectedState);
}

async function waitForCode(context, expectedState = EXPECTED_STATE) {
  const deadline = Date.now() + TIMEOUT_MS;
  while (Date.now() < deadline) {
    for (const page of context.pages()) {
      const code = await codeFromPage(page, expectedState);
      if (code) return code;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error('authorization timed out');
}

async function main() {
  let context;
  let profileDir;
  try {
    validateInputs();
    profileDir = process.env.ZCODE_OAUTH_PROFILE_DIR
      ? path.resolve(process.env.ZCODE_OAUTH_PROFILE_DIR)
      : await fs.mkdtemp(path.join(os.tmpdir(), 'zca-oauth-'));
    context = await chromium.launchPersistentContext(profileDir, {
      executablePath: CHROMIUM_PATH,
      headless: HEADLESS,
      locale: 'zh-CN',
      timezoneId: 'Asia/Shanghai',
      viewport: { width: 1272, height: 669 },
      args: [
        '--no-sandbox',
        '--disable-dev-shm-usage',
        '--window-position=0,0',
      ],
    });

    const page = context.pages()[0] || (await context.newPage());
    page.on('pageerror', (error) => diag('pageerror', error));
    await page.goto(AUTHORIZE_URL, { waitUntil: 'domcontentloaded', timeout: 30000 });
    const code = await waitForCode(context);
    const encoded = Buffer.from(code, 'utf8').toString('base64url');
    process.stdout.write(`OAUTH_CODE_B64=${encoded}\n`);
    return 0;
  } catch (error) {
    diag('failed', error);
    return 5;
  } finally {
    if (context) {
      try {
        await context.close();
      } catch (error) {
        diag('close', error);
      }
    }
    if (profileDir) {
      try {
        await fs.rm(profileDir, { recursive: true, force: true });
      } catch (error) {
        diag('profile-cleanup', error);
      }
    }
  }
}

if (require.main === module) {
  main().then((code) => process.exit(code));
}

module.exports = { readCallbackUrl };
