// Run: node --test frontend/tests/workbench.spec.cjs (Playwright on NODE_PATH).
const { test } = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("playwright");

const frontend = path.resolve(__dirname, "..");
const status = { llm_provider: "local", llm_model: "local-evidence", vector_backend: "memory", total_chunks: 3, circuit_breaker_state: "closed", embedding_model: "fixed-v1" };
const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jVZkAAAAASUVORK5CYII=", "base64");
const citation = (name, page = 1) => ({ ref: 1, chunk_id: `chunk-${name}`, document_id: name, document_name: name, page, section: "Sección técnica", kind: "text", snippet: "Presión nominal 21.4 bar", bbox: [.1, .1, .8, .2], score: .02, dense_score: .8, sparse_score: 2, page_image_url: `/api/v1/pages/run-${name}/${page}` });
const answer = (name, page = 1) => ({ answer: "La presión es 21.4 bar [1].", answer_mode: "extractive_evidence", abstained: false, evidence_coverage: "partial", model: "extractive", latency_ms: 10, timings_ms: {}, citations: [citation(name, page)], related_visuals: [], primary_visual: null, metadata: { sent_asset_ids: [], used_asset_ids: [] }, metrics: { max_cosine_similarity: .8, lexical_score: 2, top_rrf_score: .02, faithfulness_score: null } });

test("workbench security and provenance in a real browser", async (t) => {
  const server = http.createServer(async (request, response) => {
    try {
      const pathname = new URL(request.url, "http://localhost").pathname;
      if (pathname === "/openapi.json") {
        response.writeHead(200, { "Content-Type": "application/json" });
        response.end(JSON.stringify({openapi:"3.1.0",info:{title:"Fixture API",version:"test"},paths:{
          "/api/v1/documents/seed-sample":{post:{summary:"Demo de prueba",responses:{"200":{description:"OK"}}}}
        }}));
        return;
      }
      const file = path.resolve(frontend, pathname === "/" ? "index.html" : pathname === "/docs" ? "api-docs.html" : pathname.replace(/^\/static\//, ""));
      if (!file.startsWith(frontend + path.sep)) { response.writeHead(403); response.end(); return; }
      const data = await fs.readFile(file);
      response.writeHead(200, {
        "Content-Type": file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : "text/html",
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'",
      });
      response.end(data);
    } catch (_) { response.writeHead(404); response.end(); }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({ headless: true });
  t.after(async () => { await browser.close(); await new Promise((resolve) => server.close(resolve)); });

  async function setup(handler, initialSession = true) {
    const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
    const page = await context.newPage();
    page.setDefaultTimeout(8000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(String(error)));
    let authenticated = initialSession;
    await page.route("**/api/v1/**", async (route) => {
      const request = route.request();
      const pathname = new URL(request.url()).pathname;
      const json = (value, code = 200) => route.fulfill({ status: code, contentType: "application/json", body: JSON.stringify(value) });
      if (pathname === "/api/v1/auth/session") return json(authenticated ? { user: "reviewer", csrf_token: "csrf-test" } : { detail: "Authentication required" }, authenticated ? 200 : 401);
      if (pathname === "/api/v1/auth/login") { authenticated = true; return json({ user: "reviewer", csrf_token: "csrf-test" }); }
      if (pathname === "/api/v1/system/status") return json(status);
      if (pathname === "/api/v1/documents") return json([]);
      if (/^\/api\/v1\/(assets|pages)\//.test(pathname)) return route.fulfill({ contentType: "image/png", body: png });
      if (handler) return handler({ route, request, pathname, json });
      return json({ detail: "Unknown fixture route" }, 404);
    });
    await page.goto(origin);
    return { context, page, errors };
  }

  await t.test("Swagger loads under CSP without a CDN and sends session CSRF on mutations", async () => {
    const context=await browser.newContext();
    const page=await context.newPage();
    const external=[],errors=[];
    page.on("pageerror",error=>errors.push(String(error)));
    page.on("request",request=>{if(new URL(request.url()).origin!==origin) external.push(request.url());});
    let submitted;
    await page.route("**/api/v1/auth/session",route=>route.fulfill({contentType:"application/json",body:JSON.stringify({user:"reviewer",csrf_token:"docs-csrf"})}));
    await page.route("**/api/v1/documents/seed-sample",route=>{
      submitted=route.request().headers();
      return route.fulfill({contentType:"application/json",body:"{}"});
    });
    await page.goto(origin+"/docs");
    await page.locator(".swagger-ui .opblock-summary").click();
    await page.locator(".try-out__btn").click();
    const executed=page.waitForResponse(response=>response.url().endsWith("/api/v1/documents/seed-sample") && response.status()===200);
    await page.locator(".execute").click();
    await executed;
    assert.equal(submitted["x-csrf-token"],"docs-csrf");
    assert.deepEqual(external,[]);
    assert.deepEqual(errors,[]);
    await context.close();
  });

  await t.test("server Groq selection sends only the raw model, not credentials or endpoint", async () => {
    const previous={...status};
    Object.assign(status,{llm_provider:"openai",llm_model:"groq/openai/gpt-oss-120b",credentials_editable:false,openai_backend:"groq"});
    let posted;
    const {context,page,errors}=await setup(({pathname,request,json})=>{
      if(pathname==="/api/v1/system/provider") posted=request.postDataJSON();
      return json(status);
    });
    try {
      await page.locator("#btn-open-settings").click();
      assert.equal(await page.locator("#input-openai-token").isDisabled(),true);
      assert.equal(await page.locator("#input-openai-endpoint").isDisabled(),true);
      assert.equal(await page.locator("#input-openai-model").inputValue(),"openai/gpt-oss-120b");
      await page.locator('#provider-form button[type="submit"]').click();
      await page.waitForFunction(()=>document.getElementById("label-activity").textContent==="LISTO");
      assert.deepEqual(posted,{llm_provider:"openai",openai_model:"openai/gpt-oss-120b", llm_supports_images:false, openai_response_format:"auto", approve_endpoint:false});
      assert.deepEqual(errors,[]);
    } finally {
      await context.close();
      for(const key of Object.keys(status)) delete status[key];
      Object.assign(status,previous);
    }
  });

  await t.test("session login, CSRF header, no cached credentials and provider models only", async () => {
    let posted;
    const { context, page, errors } = await setup(async ({ request, pathname, json }) => {
      if (pathname === "/api/v1/system/provider") { posted = { body: request.postDataJSON(), headers: request.headers() }; return json(status); }
      return json({});
    }, false);
    await page.locator("#login-modal").waitFor({ state: "visible" });
    await page.locator("#btn-close-login").click();
    await page.locator("#login-modal").waitFor({ state: "hidden" });
    await page.locator("#btn-session").click();
    await page.locator("#login-modal").waitFor({ state: "visible" });
    await page.locator("#btn-cancel-login").click();
    await page.locator("#login-modal").waitFor({ state: "hidden" });
    await page.locator("#btn-session").click();
    await page.locator("#login-modal").waitFor({ state: "visible" });
    await page.locator("#login-username").fill("reviewer");
    await page.locator("#login-password").fill("test-password");
    await page.locator("#btn-login").click();
    await page.locator("#login-modal").waitFor({ state: "hidden" });
    assert.equal(await page.locator("#login-password").inputValue(), "");
    await page.locator("#btn-open-settings").click();
    await page.locator('#provider-form label:has(input[value="local"])').click();
    await page.locator('#provider-form button[type="submit"]').click();
    await page.waitForFunction(() => document.getElementById("label-activity").textContent === "LISTO");
    assert.deepEqual(posted.body, { llm_provider: "local" });
    assert.equal(posted.headers["x-csrf-token"], "csrf-test");
    assert.equal(await page.evaluate(() => localStorage.length), 0);
    assert.equal(await page.locator('input[id*="key"],input[id*="url"]').count(), 0);
    assert.deepEqual(errors, []);
    await context.close();
  });

  await t.test("historical [1] restores its own source after another answer", async () => {
    let index = 0;
    const { context, page, errors } = await setup(({ pathname, json }) => json(pathname.endsWith("/query") ? answer(++index === 1 ? "Manual-A" : "Manual-B", index) : {}));
    await page.locator("#chat-input").fill("Primera pregunta");
    await page.locator("#chat-form").evaluate((form) => form.requestSubmit());
    await page.locator('.msg-assistant[data-message-id="answer-1"]').waitFor();
    await page.locator("#chat-input").fill("Segunda pregunta");
    await page.locator("#chat-form").evaluate((form) => form.requestSubmit());
    await page.locator('.msg-assistant[data-message-id="answer-2"]').waitFor();
    await page.locator('[data-message-id="answer-1"] .inline-cite-btn').click();
    assert.match(await page.locator("#vis-source-pill").textContent(), /Manual-A, página 1/);
    await page.locator('[data-message-id="answer-2"] .inline-cite-btn').click();
    assert.match(await page.locator("#vis-source-pill").textContent(), /Manual-B, página 2/);
    assert.deepEqual(errors, []);
    await context.close();
  });

  await t.test("markdown and quoted metadata cannot execute scripts under script-src self", async () => {
    const data = answer("Manual '\" <img src=x onerror=window.pwned=1>");
    data.answer = '<img src=x onerror="window.pwned=1"><svg onload="window.pwned=2"></svg><script>window.pwned=3</script><a href="javascript:window.pwned=4">malicioso</a> **Dato técnico** [1]';
    const { context, page, errors } = await setup(({ json }) => json(data));
    await page.locator("#chat-input").fill("Revisar evidencia");
    await page.locator("#chat-form").evaluate((form) => form.requestSubmit());
    await page.locator(".msg-assistant .inline-cite-btn").waitFor();
    assert.equal(await page.evaluate(() => window.pwned), undefined);
    assert.equal(await page.locator('.md-body img,.md-body svg,.md-body script,[onclick],[onerror],[onload]').count(), 0);
    assert.equal(await page.locator('.md-body a[href^="javascript:"]').count(), 0);
    await page.locator(".inline-cite-btn").click();
    assert.match(await page.locator("#vis-source-pill").textContent(), /<img/);
    assert.equal(await page.evaluate(() => window.pwned), undefined);
    assert.deepEqual(errors, []);
    await context.close();
  });

  await t.test("cross-page explicit links show N/A and no false text overlay or truth percentage", async () => {
    const data = answer("Manual-A");
    data.primary_visual = { asset_id: "v1", document_id: "run-A", document_name: "Manual-A", page: 2, caption: "Figura A.3: válvula 'principal'", image_url: "/api/v1/assets/run-A/f.png", page_image_url: "/api/v1/pages/run-A/2", bbox: [.2, .2, .8, .7], anchor_bbox: [.1, .1, .8, .2], anchor_chunk_id: data.citations[0].chunk_id, distance: null, reason: "explicit_reference" };
    const { context, page, errors } = await setup(({ json }) => json(data));
    await page.locator("#chat-input").fill("Mostrar figura referenciada");
    await page.locator("#chat-form").evaluate((form) => form.requestSubmit());
    await page.locator(".inline-cite-btn").waitFor();
    assert.match(await page.locator("#vis-distance-pill").textContent(), /N\/A/);
    assert.equal(await page.locator("#bbox-svg rect").count(), 1);
    assert.match(await page.locator("#vis-caption").textContent(), /no enviada/);
    assert.match(await page.locator(".badge-grounded").textContent(), /EXTRACTIVA/);
    assert.equal(await page.locator(".msg-assistant").getByText(/FIDELIDAD|EVIDENCIA VERIFICADA|100\.0%/).count(), 0);
    assert.deepEqual(errors, []);
    await context.close();
  });

  await t.test("stopping a request says the server may continue", async () => {
    let pending;
    const { context, page, errors } = await setup(({ route, pathname, json }) => {
      if (pathname.endsWith("/query")) { pending = route; return; }
      return json({});
    });
    await page.locator("#chat-input").fill("Consulta lenta");
    await page.locator("#chat-form").evaluate((form) => form.requestSubmit());
    await page.locator(".thinking-cancel-btn").click();
    await page.locator(".badge-cancelled").waitFor();
    assert.match(await page.locator(".msg-assistant").textContent(), /puede continuar en el servidor/);
    if (pending) await pending.abort().catch(() => {});
    assert.deepEqual(errors, []);
    await context.close();
  });
});
