// 真实 Chromium + 标准库 CDP 测试；不安装 npm 依赖，不接触实际采集数据。
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const profile = await fs.mkdtemp(path.join(os.tmpdir(), 'adsb-browser-'));
const children = [];
const wait = ms => new Promise(r => setTimeout(r, ms));
let ws;
try {
  const service = spawn('python3', ['regression_tests/serve_fixture.py'], { cwd: root, env: { ...process.env, PYTHONPATH: path.join(root, 'backend') }, stdio: ['ignore', 'pipe', 'pipe'] });
  children.push(service);
  const ready = await Promise.race([
    new Promise((resolve, reject) => {
      let data = '';
      service.stdout.on('data', chunk => { data += chunk; if (data.includes('\n')) resolve(JSON.parse(data.split('\n')[0])); });
      service.once('exit', code => reject(new Error('fixture exit '+code)));
    }), wait(10000).then(() => { throw new Error('fixture timeout'); }),
  ]);
  const browser = spawn(process.env.CHROME_BIN || '/opt/google/chrome/chrome', ['--headless', '--no-sandbox', '--disable-gpu', '--remote-debugging-port=0', '--user-data-dir='+profile, 'about:blank'], { stdio: 'ignore' });
  children.push(browser);
  let port;
  for (let n=0;n<100;n++) {
    try { port = Number((await fs.readFile(path.join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]); break; } catch { await wait(100); }
  }
  assert(port, 'Chromium debugging endpoint');
  const tabs = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
  ws = new WebSocket(tabs.find(t => t.type === 'page').webSocketDebuggerUrl);
  await new Promise(r => ws.onopen = r);
  let nextId = 0; const pending = new Map(); const errors = [];
  ws.onmessage = ({data}) => {
    const msg=JSON.parse(data);
    if(msg.id) { const p=pending.get(msg.id); pending.delete(msg.id); msg.error ? p.reject(msg.error) : p.resolve(msg.result); }
    else if(msg.method==='Runtime.exceptionThrown') errors.push(msg.params.exceptionDetails);
    else if(msg.method==='Runtime.consoleAPICalled' && msg.params.type==='error') errors.push(msg.params.args);
  };
  const send = (method, params={}) => new Promise((resolve,reject)=> { const id=++nextId;pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,method,params})); });
  const run = async (code) => {
    const response = await send('Runtime.evaluate',{expression:`(async()=>{${code}})()`,awaitPromise:true,returnByValue:true});
    if(response.exceptionDetails) throw new Error(JSON.stringify(response.exceptionDetails));
    return response.result.value;
  };
  const click = async selector => run(`document.querySelector(${JSON.stringify(selector)}).click()`);
  const eventually = async (code, label) => {
    for(let i=0;i<60;i++) { if(await run('return '+code)) return; await wait(100); }
    throw new Error('Timed out: '+label);
  };
  await send('Runtime.enable'); await send('Page.enable');
  await send('Emulation.setDeviceMetricsOverride',{width:1920,height:1080,deviceScaleFactor:1,mobile:false});
  const url = `http://127.0.0.1:${ready.port}`;
  await send('Page.navigate',{url});
  await eventually(`document.querySelector('#mode-badge')?.textContent==='LIVE' && document.querySelector('#cnt-ac').textContent==='1'`,'initial live');
  assert.equal(await run(`return document.querySelector('#btn-mode').disabled`), true);
  await click('#btn-pause');
  await eventually(`document.querySelector('#pause-banner').classList.contains('visible')`,'pause banner');
  const count = await run(`return Number(document.querySelector('#cnt-msg').textContent)`);
  await wait(1100);
  assert(await run(`return Number(document.querySelector('#cnt-msg').textContent)>${count}`));
  await click('#banner-mode');
  await eventually(`document.querySelectorAll('#dataset-list input').length===2`,'historical datasets');
  assert.deepEqual(await run(`return Array.from(document.querySelectorAll('#dataset-list input'),el=>el.value)`), ['older','newer']);
  await run(`document.querySelectorAll('#dataset-list input').forEach(el=>{el.checked=true;el.dispatchEvent(new Event('change'))})`);
  await click('#ds-play');
  await eventually(`document.querySelector('#mode-badge').textContent==='REPLAY' && document.querySelector('#dataset-modal').hidden`,'replay switch');
  const api = async (endpoint, body) => (await fetch(url+endpoint, {method:'POST',headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)})).json();
  await api('/api/replay/pause');
  await eventually(`document.querySelector('#btn-pause').textContent.includes('恢复回放')`,'replay pause');
  await click('#tab-situation');
  assert.equal(await run(`return document.querySelector('#stat-count').textContent`), '2');
  await run(`document.querySelector('#search').value='no match';document.querySelector('#search').dispatchEvent(new Event('input'))`);
  assert.equal(await run(`return document.querySelector('#stat-count').textContent`), '2');
  await click('#search-clear'); await click('#tab-flights');
  await click('.ac-row');
  await eventually(`document.querySelector('#ip-trackpts').textContent!=='—'`,'selected track');
  // 持有旧数据源的轨迹响应；重建相同 hex 后释放，旧请求不能改变新轨迹。
  await run(`window.originalFetch=window.fetch;window.fetch=(...args)=>String(args[0]).startsWith('/api/track')?new Promise(resolve=>{window.releaseOld=()=>resolve(new Response(JSON.stringify({hex:'abc123',count:0,points:[],display_revision:0}))) }):window.originalFetch(...args);document.querySelector('.ac-row').click()`);
  await api('/api/replay/seek?index=102');
  await eventually(`document.querySelector('#dataset-badge').textContent.includes('newer')`,'segment change');
  await run(`window.fetch=window.originalFetch;document.querySelector('.ac-row').click()`);
  await eventually(`Number(document.querySelector('#ip-trackpts').textContent)>0`,'new track');
  const trackPoints = await run(`return document.querySelector('#ip-trackpts').textContent`);
  await run(`window.releaseOld()`);await wait(100);
  assert.equal(await run(`return document.querySelector('#ip-trackpts').textContent`), trackPoints);
  await click('#tab-situation');
  assert((await run(`return document.querySelector('#stat-position').textContent`)).startsWith('40.00000'));
  const dataBefore = (await fs.stat(path.join(ready.root,'collecting/logs/tracks-001.jsonl'))).size;
  await wait(300);
  assert((await fs.stat(path.join(ready.root,'collecting/logs/tracks-001.jsonl'))).size>dataBefore);
  await click('#sidebar-toggle');await wait(350);
  assert.equal(await run(`return Math.round(document.querySelector('#map').getBoundingClientRect().width)`),1920);
  await send('Page.reload');
  await eventually(`document.querySelector('#sidebar-frame')?.classList.contains('collapsed') && document.querySelector('.home-button')?.disabled===false`,'sidebar persistence');
  await click('#sidebar-toggle');await wait(350);
  await click('.home-button');await wait(600);
  assert(await run(`const a=document.querySelector('.rx-icon').getBoundingClientRect(),b=document.querySelector('#map').getBoundingClientRect();return Math.abs(a.x+a.width/2-b.x-b.width/2)<2 && Math.abs(a.y+a.height/2-b.y-b.height/2)<2`),'home center matches receiver marker');
  // 验证每个尺寸/主题下的缩放上限、无重叠、弹窗全视口居中与搜索复用。
  for(const width of [1920,1366,1024]) {
    await send('Emulation.setDeviceMetricsOverride',{width,height:width===1920?1080:768,deviceScaleFactor:1,mobile:false});
    await run(`document.dispatchEvent(new KeyboardEvent('keydown',{key:'0',ctrlKey:true,bubbles:true}))`);await wait(200);
    for(let i=0;i<6;i++){await run(`document.dispatchEvent(new KeyboardEvent('keydown',{key:'=',ctrlKey:true,bubbles:true}))`);await wait(60);}
    assert(await run(`const el=document.querySelector('#topbar');const boxes=Array.from(el.children).filter(e=>e.getClientRects().length).map(e=>e.getBoundingClientRect());return el.scrollWidth<=el.clientWidth+1 && !boxes.some((a,i)=>boxes.slice(i+1).some(b=>Math.min(a.right,b.right)-Math.max(a.left,b.left)>1 && Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>1))`),'topbar fits at '+width);
    const zoom = await run(`return document.querySelector('#topbar').style.zoom`);
    await send('Page.reload');await wait(500);
    assert.equal(await run(`return document.querySelector('#topbar').style.zoom`),zoom,'zoom persistence');
    if(await run(`return !document.querySelector('#search-toggle').hidden`)) {
      await click('#search-toggle');
      assert.equal(await run(`return document.activeElement.id`),'search');
      await run(`document.querySelector('#search').value='TEST';document.querySelector('#search').dispatchEvent(new Event('input'));document.querySelector('#search').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))`);
      assert.equal(await run(`return document.querySelector('#search-popover').hidden`),true);
      await click('#search-clear');
    }
    for(const theme of ['dark','light']) {
      await run(`document.documentElement.dataset.theme=${JSON.stringify(theme)}`);
      await click('#btn-mode');
      await eventually(`document.querySelectorAll('#dataset-list input').length===2`,'modal loaded');
      await wait(400);
      assert(await run(`const box=document.querySelector('#dataset-modal .sd-card').getBoundingClientRect();return Math.abs(box.x+box.width/2-innerWidth/2)<2 && Math.abs(box.y+box.height/2-innerHeight/2)<2`),'modal viewport center');
      await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape'});
      assert.equal(await run(`return document.querySelector('#dataset-modal').hidden`),true);
    }
    console.log('PASS viewport',width,'zoom',zoom);
  }
  await send('Emulation.setDeviceMetricsOverride',{width:1920,height:1080,deviceScaleFactor:1,mobile:false});
  await run(`document.dispatchEvent(new KeyboardEvent('keydown',{key:'0',ctrlKey:true,bubbles:true}))`);await wait(250);
  const wheelHandled=await run(`const e=new WheelEvent('wheel',{ctrlKey:true,deltaY:-100,bubbles:true,cancelable:true});document.querySelector('#map').dispatchEvent(e);return e.defaultPrevented`);
  assert(wheelHandled,'Ctrl+wheel intercepted before Leaflet');await wait(250);
  assert(Number(await run(`return document.querySelector('#topbar').style.zoom`))>1,'Ctrl+wheel zooms UI');
  await click('#btn-mode');await eventually(`!document.querySelector('#ds-live').disabled`,'return live available');await click('#ds-live');
  await eventually(`document.querySelector('#mode-badge').textContent==='LIVE' && document.querySelector('#dataset-modal').hidden`,'back to live');
  assert.equal(await run(`return document.querySelector('#pause-banner').classList.contains('visible')`),false);
  assert.equal(errors.length,0,JSON.stringify(errors));
  console.log('PASS live→replay→live, background disk writes, stale tracks, statistics, search, themes, layout and persistence');
} finally {
  ws?.close();
  for(const child of children.reverse()) { child.kill('SIGTERM'); await Promise.race([new Promise(r=>child.once('exit',r)),wait(1500)]); if(child.exitCode===null)child.kill('SIGKILL'); }
  await fs.rm(profile,{recursive:true,force:true,maxRetries:5,retryDelay:100});
}
