// Only manual_benchmark_server.py --auto-retry on 8797. No real model or Docker work.
import assert from 'node:assert/strict';
const {chromium}=await import(process.env.EVO_PLAYWRIGHT_MODULE);
const browser=await chromium.launch({headless:true});
const page=await browser.newPage({viewport:{width:1440,height:1000}});
const errors=[],posts=[];
page.on('pageerror',e=>errors.push(e.message));
page.on('request',r=>{if(r.method()==='POST')posts.push(new URL(r.url()).pathname);});
try {
  await page.goto('http://127.0.0.1:8797/evo');
  await page.waitForFunction(()=>document.querySelector('#dataset').options.length===2);
  const custom=await page.locator('#dataset option').evaluateAll(rows=>rows.find(o=>o.text==='Fixture').value);
  await page.selectOption('#dataset',custom);
  await page.click('#choose-tasks');await page.locator('[data-task="task-one"]').check();await page.click('.back-link');
  assert.match(await page.locator('#automation-note').textContent(),/自动检查环境及安装依赖.*2 次/);
  await page.click('#start');
  await page.waitForFunction(()=>document.querySelector('#live-progress').textContent.includes('自动重试 1 / 2'));
  assert.match(await page.locator('#progress').textContent(),/已评测 0 \/ 2/);
  assert.equal(await page.locator('#resume').isVisible(),false);
  await page.locator('[data-trial="trial-0001"]').click();
  await page.waitForFunction(()=>document.querySelector('#evidence-phase').textContent.includes('等待自动重试'));
  await page.selectOption('#attempt-select','1');
  await page.waitForFunction(()=>document.querySelector('#trace-content').textContent.includes('First attempt failed'));
  await page.click('[data-close="evidence-dialog"]');
  await page.waitForFunction(()=>document.querySelector('#run-meta').textContent.includes('已完成'));
  assert.deepEqual(posts,['/api/benchmarks/runs']);
  assert.match(await page.locator('.score-card').first().textContent(),/已重试 1 次/);
  const id=await page.locator('#history').inputValue();
  const run=await (await page.request.get(`http://127.0.0.1:8797/api/benchmarks/runs/${id}`)).json();
  assert.equal(run.preflight.ready,true);assert.equal(run.preflight.model_calls,0);
  assert.deepEqual(run.trials.map(t=>t.status),['passed','failed']);
  assert.equal(run.trials[0].attempt,2);assert.equal(run.summary[0].tokens,150);
  assert.equal(run.summary[0].unknown_usage_calls,1);
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  assert.deepEqual(errors,[]);
  console.log('PASS: one start, automatic preflight, automatic recovery without a resume request, preserved evidence/usage, live countdown, mobile. Fixture only.');
} finally {await browser.close();}
