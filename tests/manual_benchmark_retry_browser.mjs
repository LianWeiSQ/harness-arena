// Only manual_benchmark_server.py --retry on 8797; never execute a real evaluation.
import assert from 'node:assert/strict';
const {chromium}=await import(process.env.EVO_PLAYWRIGHT_MODULE);
const browser=await chromium.launch({headless:true});
const page=await browser.newPage({viewport:{width:1440,height:1000}});
const errors=[];page.on('pageerror',e=>errors.push(e.message));
try {
  await page.goto('http://127.0.0.1:8797/evo');
  await page.waitForSelector('#resume:not([hidden])');
  assert.match(await page.locator('#progress').innerText(),/已评测 2 \/ 4 · 1 异常 · 1 未执行/);
  assert.match(await page.locator('#resume').innerText(),/重试并继续 · 2/);
  assert.match(await page.locator('#recovery-info').innerText(),/保留 2 条有效结果/);
  const id=await page.locator('#history').inputValue();
  const before=await (await page.request.get(`http://127.0.0.1:8797/api/benchmarks/runs/${id}`)).json();
  await page.click('#resume');
  await page.waitForFunction(()=>document.querySelector('#live-progress').textContent.includes('下载 Node.js'));
  await page.locator('[data-trial="trial-0003"]').click();
  await page.waitForSelector('#attempt-control:not([hidden])');
  assert.equal(await page.locator('#attempt-select').inputValue(),'2');
  await page.selectOption('#attempt-select','1');
  await page.waitForFunction(()=>document.querySelector('#trace-content').textContent.includes('Original fixture evidence'));
  assert.equal(await page.locator('#evidence-phase').textContent(),'执行异常');
  await page.selectOption('#attempt-select','2');
  await page.waitForFunction(()=>document.querySelector('#installation-log').textContent.includes('Fixture download'));
  await page.click('[data-close="evidence-dialog"]');
  await page.waitForFunction(()=>document.querySelector('#run-meta').textContent.includes('已完成'));
  assert.equal(await page.locator('#progress').innerText(),'已评测 4 / 4');
  assert.equal(await page.locator('#resume').isVisible(),false);
  assert.equal(await page.locator('#history').inputValue(),id);
  const after=await (await page.request.get(`http://127.0.0.1:8797/api/benchmarks/runs/${id}`)).json();
  for(let i=0;i<2;i++){
    delete before.trials[i].progress;delete after.trials[i].progress;
    assert.deepEqual(after.trials[i],before.trials[i]);
  }
  assert.deepEqual(after.trials.map(t=>t.status),['passed','failed','passed','failed']);
  assert.equal(after.trials[2].attempt,2);
  assert.deepEqual(after.summary.map(e=>e.tokens),[200,150]);
  await page.reload();
  await page.waitForSelector('[data-trial="trial-0003"]');
  await page.locator('[data-trial="trial-0003"]').click();
  await page.waitForSelector('#attempt-control:not([hidden])');
  await page.selectOption('#attempt-select','1');
  await page.waitForFunction(()=>document.querySelector('#trace-content').textContent.includes('Original fixture evidence'));
  await page.click('[data-close="evidence-dialog"]');
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  assert.deepEqual(errors,[]);
  console.log('PASS: retry/resume, live progress, prior attempt evidence, preserved scores, cumulative usage, reload, mobile. Fixture only.');
} finally {await browser.close();}
