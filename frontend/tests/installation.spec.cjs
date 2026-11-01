// Real HTTP + persistent SQLite/Qdrant + browser; no cloud credentials or API mocks.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const net = require('node:net');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const { chromium } = require('playwright');

test('first owner, demo PDF, cited answer, provider probe, logout and persisted login', { timeout: 90000 }, async (t) => {
  const root = path.resolve(__dirname, '../..');
  const data = await fs.mkdtemp(path.join(os.tmpdir(), 'aetherrag-browser-v4-'));
  const socket = net.createServer();
  await new Promise(resolve => socket.listen(0, '127.0.0.1', resolve));
  const port = socket.address().port;
  await new Promise(resolve => socket.close(resolve));
  const url = 'http://127.0.0.1:' + port;
  const python = process.env.AETHERRAG_TEST_PYTHON || (process.platform === 'win32'
    ? path.join(root, '.venv/Scripts/python.exe') : 'python');
  let child;
  async function stop() {
    if (child && child.exitCode === null) {
      const exited = once(child, 'exit');
      const response = await fetch(url + '/__fixture-stop', { method: 'POST' });
      assert.equal(response.status, 200);
      let timer;
      try {
        await Promise.race([exited, new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error('Fixture failed to stop gracefully')), 10000);
        })]);
      } finally {
        clearTimeout(timer);
      }
    }
  }
  async function start() {
    const runner = 'import uvicorn\nfrom app.main import create_app\napp=create_app()\n' +
      'server=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=' + port + ',log_level="error"))\n' +
      '@app.post("/__fixture-stop")\nasync def stop():\n server.should_exit=True\n return {}\n' +
      'server.run()\n';
    child = spawn(python, ['-B', '-c', runner], {
      cwd: root, windowsHide: true, stdio: 'ignore', env: { ...process.env,
        PYTHONPATH: path.join(root, 'backend'), PYTHONDONTWRITEBYTECODE: '1',
        AETHERRAG_ENV_FILE: path.join(data, 'no.env'), ENVIRONMENT: 'development', DATA_DIR: data,
        LLM_PROVIDER: 'local', EMBEDDING_PROVIDER: 'local', VECTOR_BACKEND: 'embedded',
        DATABASE_URL: '', QUEUE_BACKEND: 'background', AUTH_ENABLED: 'true', SESSION_SECURE: 'false',
        AUTO_SEED_SAMPLE_MANUAL: 'false', GEMINI_API_KEY: '', OPENAI_API_KEY: '', GROQ_API_KEY: '',
        ANTHROPIC_API_KEY: '', OPERATOR_PASSWORD_HASH: '', SESSION_SECRET: '', DISPATCHER_INTERVAL_SECONDS: '.1' }
    });
    for (let i = 0; i < 150; i++) {
      if (child.exitCode !== null) throw new Error('Fixture API exited: ' + child.exitCode);
      try { if ((await fetch(url + '/api/v1/system/live')).ok) return; } catch (_) { }
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error('Fixture API did not start');
  }
  const browser = await chromium.launch({ headless: true });
  t.after(async () => { await browser.close(); await stop(); });
  await start();
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  page.setDefaultTimeout(20000);
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  await page.goto(url);
  await page.locator('#register-form').waitFor({ state: 'visible' });
  await page.waitForFunction(() => !location.hash);
  await page.locator('#register-username').fill('reviewer');
  await page.locator('#register-password').fill('Unique-Browser-Password-48291');
  await page.locator('#register-confirm').fill('Unique-Browser-Password-48291');
  await page.locator('#btn-register').click();
  await page.locator('#login-modal').waitFor({ state: 'hidden' });
  assert.match(await page.locator('#btn-session').textContent(), /reviewer/);
  assert.match(await page.locator('#label-provider').textContent(), /4\.0\.0/);
  await page.locator('#btn-seed-demo').click();
  await page.waitForFunction(() => document.querySelectorAll('.doc-card').length === 1);
  await page.locator('#chat-input').fill('¿A qué temperatura dispara el sensor ST-104?');
  await page.locator('#btn-send').click();
  await page.waitForFunction(() => document.querySelectorAll('.citation-card').length > 0);
  await page.locator('#btn-open-settings').click();
  await page.locator('#btn-test-provider').click();
  await page.waitForFunction(() => document.getElementById('provider-test-message').textContent.includes('verificados'));
  await page.locator('#btn-close-settings').click();
  await page.locator('#btn-logout').click();
  await page.locator('#login-modal').waitFor({ state: 'visible' });
  assert.equal((await page.request.get(url + '/api/v1/documents')).status(), 401);
  await stop();
  await start();
  await page.reload();
  await page.locator('#login-form').waitFor({ state: 'visible' });
  await page.locator('#login-username').fill('reviewer');
  await page.locator('#login-password').fill('Unique-Browser-Password-48291');
  await page.locator('#btn-login').click();
  await page.locator('#login-modal').waitFor({ state: 'hidden' });
  await page.waitForFunction(() => document.querySelectorAll('.doc-card').length === 1);
  const docs = await page.request.get(url + '/docs');
  assert.equal(docs.status(), 200);
  assert.match(await docs.text(), /api-docs-init\.js/);
  assert.deepEqual(errors, []);
});
