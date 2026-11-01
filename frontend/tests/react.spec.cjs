// React migration: asynchronous lifetimes, privacy and panel ergonomics.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const http=require('node:http');
const fs=require('node:fs/promises');
const path=require('node:path');
const {chromium}=require('playwright');
const frontend=path.resolve(__dirname,'..');
const status={llm_provider:'local',llm_model:'local-extractive-evidence',vector_backend:'memory',total_chunks:2,
  embedding_model:'fixture',circuit_breaker_state:'closed',credentials_editable:true,app_version:'4.1.0'};
const docs=['a','b'].map(id=>({document_id:id,file_name:id+'.pdf',page_count:1,chunk_count:2,visual_count:0,table_count:0,sha256:id,status:'completed'}));
const response=(text='Dato documental 12 bar [1].')=>({answer:text,answer_mode:'extractive_evidence',abstained:false,
  evidence_coverage:'partial',model:'local-extractive-evidence',latency_ms:20,timings_ms:{retrieval_ms:10,generation_ms:10},
  citations:[{ref:1,chunk_id:'chunk-a',document_id:'a',document_name:'a.pdf',page:1,kind:'text',section:'Fuente',
    snippet:'Dato documental 12 bar',bbox:[.1,.1,.8,.2],score:.02,page_image_url:'/api/v1/pages/a/1'}],
  metadata:{accepted_claims:1,rejected_claims:0},metrics:{max_cosine_similarity:.8,top_rrf_score:.02,lexical_score:.2}});
test('React lifetimes and usability under CSP',async t=>{
  const server=http.createServer(async(req,res)=>{
    try{
      const pathname=new URL(req.url,'http://localhost').pathname;
      const file=path.resolve(frontend,pathname==='/' ? 'index.html' : pathname.replace(/^\/static\//,''));
      if(!file.startsWith(frontend+path.sep)) {res.writeHead(403);res.end();return;}
      res.writeHead(200,{'Content-Type':file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html',
        'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'"});
      res.end(await fs.readFile(file));
    }catch{res.writeHead(404);res.end();}
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const origin='http://127.0.0.1:'+server.address().port;
  const browser=await chromium.launch({headless:true});
  t.after(async()=>{await browser.close();await new Promise(resolve=>server.close(resolve));});
  async function setup(handler,clock=false){
    const context=await browser.newContext({viewport:{width:1600,height:1000},reducedMotion:'reduce'});
    const page=await context.newPage(),errors=[];
    page.setDefaultTimeout(12000);page.on('pageerror',error=>errors.push(String(error)));
    if(clock) await page.clock.install();
    await page.route('**/api/v1/**',async route=>{
      const request=route.request(),pathname=new URL(request.url()).pathname;
      const json=(body,code=200)=>route.fulfill({status:code,contentType:'application/json',body:JSON.stringify(body)});
      if(pathname.endsWith('/auth/setup')) return json({registration_required:false});
      if(pathname.endsWith('/system/version')) return json({version:'4.1.0',environment:'test',build_id:'fixture'});
      if(pathname.endsWith('/auth/session')) return json({user:'reviewer',csrf_token:'initial'});
      if(pathname.endsWith('/auth/login')) return json({user:'reviewer',csrf_token:'renewed'});
      if(pathname.endsWith('/auth/logout')) return json({});
      if(pathname.endsWith('/system/status')) return json(status);
      if(pathname==='/api/v1/documents') return json(docs);
      if(handler) return handler({route,request,pathname,json});
      return json({});
    });
    await page.goto(origin);
    await page.waitForFunction(()=>document.getElementById('btn-session').textContent.includes('reviewer'));
    return {page,context,errors};
  }
  const ask=async(page,text)=>{
    await page.locator('#chat-input').fill(text);
    await page.locator('#chat-form').evaluate(form=>form.requestSubmit());
  };
  await t.test('four second pulse threshold preserves elapsed time and cancellation',async()=>{
    let pending;
    const {page,context,errors}=await setup(({route,pathname,json})=>{
      if(pathname.endsWith('/query')) {pending=route;return;}return json({});
    },true);
    try{
      await ask(page,'Consulta lenta');
      await page.locator('.thinking-cancel-btn').waitFor();
      assert.equal(await page.locator('#pill-activity').getAttribute('data-state'),'working');
      await page.clock.fastForward(4100);
      assert.equal(await page.locator('#pill-activity').getAttribute('data-state'),'warning');
      assert(Number.parseFloat(await page.locator('#timer-activity').textContent())>=4);
      await page.locator('.thinking-cancel-btn').click();
      await page.locator('.badge-cancelled').waitFor();
      assert.match(await page.locator('.msg-assistant').textContent(),/puede continuar en el servidor/);
      if(pending) await pending.abort().catch(()=>{});
      assert.deepEqual(errors,[]);
    }finally{await context.close();}
  });
  await t.test('switching documents back while a query runs cannot restore old history',async()=>{
    let pending;const queries=[];
    const {page,context,errors}=await setup(({route,request,pathname,json})=>{
      if(pathname.endsWith('/query')) {queries.push(request.postDataJSON());if(queries.length===1){pending=route;return;}return json(response());}
      return json({});
    });
    try{
      await page.getByRole('checkbox',{name:'Consultar a.pdf',exact:true}).check();
      await ask(page,'Consulta inicial');
      await page.waitForFunction(()=>document.querySelector('.thinking-cancel-btn'));
      await page.getByRole('checkbox',{name:'Consultar a.pdf',exact:true}).uncheck();
      await page.getByRole('checkbox',{name:'Consultar b.pdf',exact:true}).check();
      await page.getByRole('checkbox',{name:'Consultar b.pdf',exact:true}).uncheck();
      await page.getByRole('checkbox',{name:'Consultar a.pdf',exact:true}).check();
      await pending.fulfill({contentType:'application/json',body:JSON.stringify(response())});
      await page.locator('.inline-cite-btn').waitFor();
      await ask(page,'Consulta con selección nueva');
      await page.waitForFunction(()=>document.querySelectorAll('.inline-cite-btn').length===2);
      assert.deepEqual(queries[1].history,[]);
      assert.deepEqual(queries[1].document_ids,['a']);
      assert.deepEqual(errors,[]);
    }finally{await context.close();}
  });
  await t.test('provider failures show red briefly without inventing an evidence abstention',async()=>{
    const {page,context,errors}=await setup(({json})=>json({detail:'Servicio de prueba temporalmente indisponible'},503),true);
    try {
      await ask(page,'Consulta con fallo de proveedor');
      await page.waitForFunction(()=>document.getElementById('pill-activity').dataset.state==='error');
      assert.equal(await page.locator('.badge-abstained').count(),0);
      assert.match(await page.locator('.msg-assistant').textContent(),/Servicio de prueba/);
      const elapsed=await page.locator('#timer-activity').textContent();
      await page.clock.fastForward(5000);
      assert.equal(await page.locator('#timer-activity').textContent(),elapsed);
      await page.clock.fastForward(1100);
      await page.waitForFunction(()=>document.getElementById('pill-activity').dataset.state==='idle');
      assert.deepEqual(errors,[]);
    } finally {await context.close();}
  });
  await t.test('editable credentials clear before the server responds and are never persisted',async()=>{
    let pending,payload;
    const {page,context,errors}=await setup(({route,request,pathname,json})=>{
      if(pathname.endsWith('/provider')) {pending=route;payload=request.postDataJSON();return;}return json({});
    });
    try{
      await page.locator('#btn-open-settings').click();
      await page.locator('input[name="llm_provider"][value="openai"]').check();
      await page.locator('#input-openai-token').fill('synthetic-token');
      await page.locator('#provider-form button[type="submit"]').click();
      await page.waitForFunction(()=>document.getElementById('input-openai-token').value==='');
      assert.equal(payload.openai_api_key,'synthetic-token');
      assert.deepEqual(await page.evaluate(()=>[localStorage.length,sessionStorage.length]),[0,0]);
      await pending.fulfill({contentType:'application/json',body:JSON.stringify(status)});
      await page.locator('#settings-modal').waitFor({state:'hidden'});
      assert.deepEqual(errors,[]);
    }finally{await context.close();}
  });
  await t.test('matching filenames never justify a physical overlay across different source pages',async()=>{
    const data=response();
    data.primary_visual={asset_id:'v',document_id:'b',document_name:'a.pdf',page:1,caption:'Figura relacionada',
      image_url:'/api/v1/assets/b/figure.png',page_image_url:'/api/v1/pages/b/1',bbox:[.2,.3,.8,.7],
      anchor_bbox:[.1,.1,.8,.2],anchor_chunk_id:'chunk-a',distance:.2,reason:'explicit_reference'};
    const {page,context,errors}=await setup(({json})=>json(data));
    try {
      await ask(page,'Inspeccionar fuentes');
      await page.locator('.inline-cite-btn').waitFor();
      assert.match(await page.locator('#vis-distance-pill').textContent(),/N\/A/);
      assert.equal(await page.locator('#bbox-svg rect').count(),1);
      assert.deepEqual(errors,[]);
    } finally {await context.close();}
  });
  await t.test('expired sessions erase private UI and renew CSRF without replaying a query',async()=>{
    let count=0,second;
    const {page,context,errors}=await setup(({request,pathname,json})=>{
      if(pathname.endsWith('/query')) {count++;if(count===1)return json({detail:'Expired'},401);second={body:request.postDataJSON(),headers:request.headers()};return json(response());}
      return json({});
    });
    try{
      await ask(page,'Consulta que expira');
      await page.locator('#login-modal').waitFor({state:'visible'});
      assert.equal(await page.locator('.msg-assistant').count(),0);
      assert.equal(await page.locator('.doc-card').count(),0);
      await page.locator('#login-username').fill('reviewer');
      await page.locator('#login-password').fill('synthetic-password');
      await page.locator('#btn-login').click();
      await page.locator('#login-modal').waitFor({state:'hidden'});
      await ask(page,'Consulta nueva');
      await page.locator('.inline-cite-btn').waitFor();
      assert.equal(count,2);assert.equal(second.headers['x-csrf-token'],'renewed');assert.deepEqual(second.body.history,[]);
      assert.deepEqual(errors,[]);
    }finally{await context.close();}
  });
  await t.test('many answers keep their height, eight telemetry cells and manual scroll position',async()=>{
    const {page,context,errors}=await setup(({json})=>json(response(('Explicación documental extensa con referencia [1].\n\n').repeat(8))));
    try{
      await ask(page,'Primera consulta');
      await page.locator('[data-message-id="answer-1"] .verification-telemetry').waitFor();
      await page.waitForFunction(()=>{const el=document.getElementById('chat-stream');return el.scrollHeight-el.scrollTop-el.clientHeight<72;});
      const first=await page.locator('[data-message-id="answer-1"]').evaluate(el=>el.getBoundingClientRect().height);
      for(let i=2;i<=5;i++){
        await ask(page,'Consulta '+i);
        await page.locator('[data-message-id="answer-'+i+'"] .verification-telemetry').waitFor();
      }
      const last=await page.locator('[data-message-id="answer-1"]').evaluate(el=>el.getBoundingClientRect().height);
      assert(last>=first-2);
      assert.equal(await page.locator('[data-message-id="answer-1"] .telemetry-cell').count(),8);
      assert.match(await page.locator('#quick-prompts').getAttribute('class'),/collapsed/);
      await page.locator('#chat-stream').evaluate(el=>{el.scrollTop=0;});
      await page.locator('#btn-jump-latest').waitFor({state:'visible'});
      await ask(page,'Consulta mientras reviso el inicio');
      await page.locator('[data-message-id="answer-6"] .verification-telemetry').waitFor();
      assert((await page.locator('#chat-stream').evaluate(el=>el.scrollTop))<10);
      await page.locator('#btn-jump-latest').click();
      await page.waitForFunction(()=>{const el=document.getElementById('chat-stream');return el.scrollHeight-el.scrollTop-el.clientHeight<72;});
      await page.locator('#btn-theme').click();
      assert.equal(await page.locator('html').getAttribute('data-theme'),'light');
      assert.equal(await page.evaluate(()=>localStorage.length),0);
      assert.deepEqual(errors,[]);
    }finally{await context.close();}
  });
});
