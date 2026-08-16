'use strict';

const path = require('path');
const { chromium } = require('playwright-core');

const SCENE = process.argv[2] || '11xygtvd';
const REGION = process.argv[3] || 'sgp';
const PREFIX = process.argv[4] || 'no8xfe';
const CHROMIUM_PATH = process.env.ZCODE_CHROMIUM_PATH || '/usr/bin/chromium';
const PROFILE_DIR = process.env.ZCODE_CHROMIUM_PROFILE_DIR || '/data/chromium-profile';
const HEADLESS = process.env.ZCODE_CAPTCHA_BROWSER_HEADLESS === '1';
const TIMEOUT_MS = Number.parseInt(process.env.ZCODE_CAPTCHA_BROWSER_TIMEOUT || '120000', 10);
const PAGE_URL = 'https://zcode.z.ai/__zca_captcha__';
const DIAG_MAX = 200;

function diag(tag, detail) {
  let text = '';
  if (detail instanceof Error) text = detail.message || detail.name || '';
  else if (typeof detail === 'string') text = detail;
  else if (detail != null) text = Object.prototype.toString.call(detail);
  text = String(text).replace(/\s+/g, ' ').slice(0, DIAG_MAX);
  process.stderr.write(`[browser-solver] ${tag}${text ? `: ${text}` : ''}\n`);
}

const html = `<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>ZCode verification</title>
    <style>
      body { margin: 0; min-height: 100vh; display: grid; place-items: center; font-family: sans-serif; }
      #captcha-wrap { width: 420px; min-height: 180px; }
      #captcha-button { width: 360px; height: 42px; }
    </style>
    <script>
      window.AliyunCaptchaConfig = ${JSON.stringify({ region: REGION, prefix: PREFIX })};
    </script>
    <script src="https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js"></script>
  </head>
  <body>
    <main id="captcha-wrap">
      <div id="captcha-element"></div>
      <button id="captcha-button" type="button">验证</button>
    </main>
  </body>
</html>`;

async function main() {
  let context;
  try {
    context = await chromium.launchPersistentContext(path.resolve(PROFILE_DIR), {
      executablePath: CHROMIUM_PATH,
      headless: HEADLESS,
      locale: 'zh-CN',
      timezoneId: 'Asia/Shanghai',
      // Keep the outer Chromium window inside the 1280x800 Xvfb desktop.
      // Chromium adds 7px horizontally and 130px vertically around this
      // content viewport; the resulting X11 window is 1279x799.
      viewport: { width: 1272, height: 669 },
      args: [
        '--no-sandbox',
        '--disable-dev-shm-usage',
        '--window-position=0,0',
      ],
    });

    const page = context.pages()[0] || (await context.newPage());
    page.on('pageerror', (error) => diag('pageerror', error));
    page.on('console', (message) => {
      if (message.text() === 'ZCA_INTERACTIVE_REQUIRED') diag('interactive-displayed');
    });
    await page.route(PAGE_URL, (route) =>
      route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: html }),
    );
    await page.goto(PAGE_URL, { waitUntil: 'domcontentloaded', timeout: TIMEOUT_MS });
    await page.waitForFunction(() => typeof window.initAliyunCaptcha === 'function', null, {
      timeout: Math.min(TIMEOUT_MS, 15000),
    });

    const result = await page.evaluate(
      ({ scene, region, prefix, timeoutMs }) =>
        new Promise((resolve) => {
          let settled = false;
          const finish = (value) => {
            if (settled) return;
            settled = true;
            resolve(value);
          };
          const timer = window.setTimeout(
            () => finish({ status: 'timeout' }),
            timeoutMs,
          );
          const complete = (value) => {
            window.clearTimeout(timer);
            finish(value);
          };

          let captchaInstance = null;
          let interactiveShown = false;
          const readResult = (value) => {
            if (!value || typeof value !== 'object') return {};
            return {
              success: typeof value.success === 'boolean' ? value.success : undefined,
              verifyResult:
                typeof value.verifyResult === 'boolean' ? value.verifyResult : undefined,
              verifyCode:
                typeof value.verifyCode === 'string'
                  ? value.verifyCode
                  : typeof value.VerifyCode === 'string'
                    ? value.VerifyCode
                    : undefined,
              verifyParam:
                typeof value.captchaVerifyParam === 'string'
                  ? value.captchaVerifyParam
                  : typeof value.CaptchaVerifyParam === 'string'
                    ? value.CaptchaVerifyParam
                    : undefined,
            };
          };

          window.initAliyunCaptcha({
            SceneId: scene,
            mode: 'popup',
            region,
            prefix,
            language: 'cn',
            element: '#captcha-element',
            button: '#captcha-button',
            captchaLogoImg: '',
            showErrorTip: false,
            delayBeforeSuccess: false,
            slideStyle: { width: 360, height: 40 },
            getInstance(instance) {
              captchaInstance = instance;
              try {
                instance.startTracelessVerification();
              } catch (_error) {
                complete({ status: 'error', reason: 'start failed' });
              }
            },
            success(verifyParam) {
              complete({ status: 'success', verifyParam });
            },
            fail(value) {
              const result = readResult(value);
              const terminalPass =
                (result.success === true && result.verifyResult === true) ||
                result.verifyCode === 'T006';
              if (terminalPass && result.verifyParam) {
                complete({ status: 'success', verifyParam: result.verifyParam });
                return;
              }
              const needsInteractive =
                (result.success === true && result.verifyResult === false) ||
                (terminalPass && !result.verifyParam);
              if (!needsInteractive) {
                complete({ status: 'error', reason: 'verification failed' });
                return;
              }
              interactiveShown = true;
              console.info('ZCA_INTERACTIVE_REQUIRED');
              try {
                if (captchaInstance && typeof captchaInstance.show === 'function') {
                  captchaInstance.show();
                } else {
                  document.querySelector('#captcha-button')?.click();
                }
              } catch (_error) {
                complete({ status: 'error', reason: 'interactive display failed' });
              }
            },
            onError() {
              complete({ status: 'error', reason: 'sdk error' });
            },
          });

          window.setTimeout(() => {
            if (interactiveShown) complete({ status: 'interactive' });
          }, timeoutMs - 100);
        }),
      { scene: SCENE, region: REGION, prefix: PREFIX, timeoutMs: TIMEOUT_MS },
    );

    if (result && result.status === 'success' && typeof result.verifyParam === 'string') {
      process.stdout.write(`VERIFY_PARAM=${result.verifyParam}\n`);
      return 0;
    }
    if (result && result.status === 'interactive') {
      diag('interactive-required');
      return 6;
    }
    diag(result && result.status === 'timeout' ? 'timeout' : 'verification-error');
    return 5;
  } catch (error) {
    diag('fatal', error);
    return 3;
  } finally {
    if (context) {
      try {
        await context.close();
      } catch (error) {
        diag('close', error);
      }
    }
  }
}

main().then((code) => process.exit(code));
