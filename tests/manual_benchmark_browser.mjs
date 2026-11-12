// Run only against manual_benchmark_server.py. Never submit to the real workspace.
import assert from 'node:assert/strict';
const modulePath = process.env.EVO_PLAYWRIGHT_MODULE;
if (!modulePath) throw new Error('Set EVO_PLAYWRIGHT_MODULE to the Playwright entrypoint');
const { chromium } = await import(modulePath);
const browser = await chromium.launch({headless:true});
const page = await browser.newPage({viewport:{width:1440,height:1000}});
const errors=[];page.on('pageerror', e=>errors.push(e.message));
page.on('console', e=>{if(e.type()==='error') errors.push(e.text());});
try {
  await page.goto('http://127.0.0.1:8797/evo');
  await page.waitForFunction(()=>document.querySelector('#dataset').options.length===2);
  const custom = await page.locator('#dataset option').evaluateAll(options=>options.find(o=>o.text==='Fixture').value);
  await page.selectOption('#dataset', custom);
  assert.equal(await page.locator('#start').isEnabled(), false);
  await page.click('#choose-tasks');
  await page.locator('[data-task="task-one"]').check();
  await page.click('.back-link');
  assert.equal(await page.locator('#trial-count').innerText(),'2');
  await page.click('#start');
  await page.waitForFunction(()=>document.querySelector('#run-meta').textContent.includes('已完成'));
  assert.equal(await page.locator('.score-card').count(),2);
  assert.match(await page.locator('.score-card').first().innerText(),/100.0%/);
  assert.match(await page.locator('.score-card').nth(1).innerText(),/0.0%/);
  await page.locator('[data-trial]').first().click();
  await page.waitForFunction(()=>document.querySelector('#evidence-content').textContent.includes('Controlled browser fixture'));
  assert.match(await page.locator('#evidence-content').innerText(),/Controlled browser fixture/);
  await page.click('[data-close="evidence-dialog"]');
  await page.reload();
  await page.waitForSelector('.score-card');
  assert.equal(await page.locator('.score-card').count(),2);
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  assert.deepEqual(errors,[]);
  console.log('PASS: selection, submit, comparisons, evidence, reload, mobile; fixture only.');
} finally {await browser.close();}
