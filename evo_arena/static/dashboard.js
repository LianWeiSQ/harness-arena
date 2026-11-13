'use strict';
const $=id=>document.getElementById(id);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const statuses={queued:'待执行',preparing:'准备中',running:'执行中',cancelling:'停止中',completed:'已完成',incomplete:'未完整完成',interrupted:'已中断',cancelled:'已停止',not_run:'未执行',passed:'通过',failed:'未通过',scored:'原始评分',error:'执行异常',mixed:'部分通过'};
let selectedRun=new URLSearchParams(location.search).get('run')||'',loading=false;
const label=r=>r==='pi'?'Pi':r==='dsh'?'DSH':r;
const color=r=>r==='dsh'?'runner-dsh':'runner-pi';
const count=n=>Number(n).toLocaleString('zh-CN');
const compact=n=>n>=1000000?(n/1000000).toFixed(2)+'M':n>=1000?(n/1000).toFixed(1)+'k':count(n);
const duration=n=>{if(n==null)return '—';const s=Math.round(n);return s>=3600?`${Math.floor(s/3600)}h ${Math.floor(s%3600/60)}m`:s>=60?`${Math.floor(s/60)}m ${s%60}s`:`${s}s`;};
const date=v=>new Date(v).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
const runLink=(id,trial)=>'/evo?'+new URLSearchParams({run:id,...(trial?{trial}:{})});
function unscored(e){return e.errors?'含执行异常':e.pending?'等待评测完成':e.scored?'保留原始得分':e.not_run?'未完整执行':'尚无完整评分';}
function setHTML(id,html){if($(id).innerHTML!==html)$(id).innerHTML=html;}
function renderChart(id,entries,key,format){
  const valid=entries.filter(e=>e.score!==null&&e[key]!=null&&(key!=='tokens'||e.usage_known));
  const max=Math.max(1,...valid.map(e=>e[key]));
  setHTML(id,valid.length?valid.map(e=>`<div class="chart-row ${color(e.runner)}"><div class="chart-label"><span><i class="dot"></i> ${esc(label(e.runner))}</span><b>${esc(format(e[key]))}</b></div><span class="chart-model">${esc(e.model)}</span><progress class="metric-bar" value="${e[key]}" max="${max}" aria-label="${esc(label(e.runner)+' '+e.model+' '+format(e[key]))}"></progress></div>`).join(''):'<p class="chart-empty">完整结果生成后显示</p>');
}
function render(data){
  $('empty').hidden=!!data.run;$('content').hidden=!data.run;
  $('active').hidden=!data.active_runs.length;
  if(data.active_runs.length)setHTML('active',data.active_runs.map(r=>`<span>${esc(r.dataset)} · ${esc(statuses[r.status]||r.status)}</span><a href="${esc(runLink(r.id))}">查看实时执行 →</a>`).join(''));
  if(!data.run)return;
  const r=data.run,entries=data.entries;
  $('dataset').textContent=r.dataset;
  $('scope-badge').textContent=r.is_subset?'子集评测':'全量任务';
  $('coverage').textContent=`${r.task_count} / ${r.dataset_size} 题${r.revision?' · '+r.revision.slice(0,12):''}`;
  setHTML('run-select',data.runs.map(v=>`<option value="${esc(v.id)}">${esc(date(v.created))} · ${esc(statuses[v.status]||v.status)} · ${v.task_count} 题</option>`).join(''));
  $('run-select').value=r.id;
  $('entry-count').textContent=entries.length;$('task-count').textContent=r.task_count;
  $('repeat-count').textContent=`每题 ${r.repeats} 次 · 相同任务与预算`;
  $('graded-count').textContent=`${r.graded} / ${r.total}`;$('run-state').textContent=statuses[r.status]||r.status;
  $('run-count').textContent=data.evaluation_count;
  setHTML('scores',entries.map(e=>`<tr class="${color(e.runner)}" data-entry="${esc(e.id)}"><td class="rank">${e.rank===null?'—':String(e.rank).padStart(2,'0')}</td><td><div class="harness-name"><i class="dot"></i><b>${esc(label(e.runner))}</b><span class="version">${esc(e.versions.join(' / '))}</span></div><span class="model-name">${esc(e.model)}</span></td><td class="score-cell">${e.score===null?`<span class="score-number">—</span><small>${esc(unscored(e))}</small>`:`<span class="score-number">${e.score.toFixed(1)}</span><small>分</small><progress class="score-bar" max="100" value="${e.score}" aria-label="${esc(label(e.runner))} 评分 ${e.score.toFixed(1)}"></progress>`}</td><td class="number">${e.passed} / ${e.total}<span class="secondary">${e.errors?e.errors+' 次异常':e.not_run?e.not_run+' 次未完成':e.failed?e.failed+' 次未通过':e.pending?'执行中':e.scored?'自定义奖励':'通过 / 计划执行'}</span></td><td class="number">${duration(e.mean_seconds)}</td><td class="number" title="已记录 ${count(e.tokens)} Token">${e.usage_known?'':'≥ '}${compact(e.tokens)}<span class="secondary">${e.calls} 次请求${e.usage_known?'':' · 用量未完整'}</span></td><td><a class="row-link" href="${esc(runLink(r.id,data.tasks.flatMap(t=>t.cells).find(c=>c.entry===e.id&&c.trial_ids.length)?.trial_ids[0]))}">详情 ↗</a></td></tr>`).join(''));
  $('score-note').textContent=(r.is_subset?`本场仅覆盖 ${r.task_count} / ${r.dataset_size} 题，分数不代表全套成绩。`:'分数仅代表本场评测。')+' 异常、缺失或非二值结果不折算分数；未完整评分的组合不排名。';
  renderChart('time-chart',entries,'mean_seconds',duration);renderChart('token-chart',entries,'tokens',compact);
  $('recent-count').textContent=`共 ${data.runs.length} 场`;
  setHTML('recent-runs',data.runs.slice(0,4).map(v=>`<a class="recent-row" href="/?run=${encodeURIComponent(v.id)}"><time>${esc(date(v.created))}</time><span class="recent-title">${esc(v.dataset)}<small>${v.task_count} 题 · ${v.entry_count} 组合 · ${esc([...new Set(v.combinations.map(e=>e.model))].join(' / '))}</small></span><span class="run-status status-${esc(v.status)}">${esc(statuses[v.status]||v.status)} ↗</span></a>`).join(''));
  $('view-run').href=runLink(r.id);
  setHTML('task-matrix',`<table><thead><tr><th>任务</th>${entries.map(e=>`<th>${esc(label(e.runner))}<small>${esc(e.model)}</small></th>`).join('')}</tr></thead><tbody>${data.tasks.map(t=>`<tr><td>${esc(t.id)}<span class="secondary">${esc(t.category)}</span></td>${t.cells.map(c=>{const text=['passed','failed','mixed'].includes(c.status)?`${c.passed} / ${c.total}`:statuses[c.status]||c.status;return `<td><a class="task-result status-${esc(c.status)}" href="${esc(runLink(r.id,c.trial_ids[0]))}" title="${esc(c.rewards.length?JSON.stringify(c.rewards):statuses[c.status])}">${esc(text)}</a></td>`;}).join('')}</tr>`).join('')}</tbody></table>`);
}
async function refresh(){
  if(loading)return;loading=true;const requested=selectedRun;
  try{
    const response=await fetch('/api/dashboard'+(requested?'?run='+encodeURIComponent(requested):''),{signal:AbortSignal.timeout(15000)});
    const data=await response.json();if(!response.ok)throw new Error(data.error||'读取评分失败');
    if(requested!==selectedRun)return;
    render(data);$('error').hidden=true;$('sync-status').textContent='本地结果 · 自动更新';
  }catch(error){$('error').textContent=error.message;$('error').hidden=false;$('sync-status').textContent='更新失败';}
  finally{loading=false;}
}
$('run-select').onchange=()=>{selectedRun=$('run-select').value;window.history.replaceState(null,'','/?run='+encodeURIComponent(selectedRun));refresh();};
try{document.documentElement.dataset.theme=localStorage.getItem('arena-theme')||'light';}catch{}
$('theme-toggle').onclick=()=>{const theme=document.documentElement.dataset.theme==='dark'?'light':'dark';document.documentElement.dataset.theme=theme;try{localStorage.setItem('arena-theme',theme);}catch{}};
refresh();setInterval(()=>{if(!document.hidden)refresh();},8000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
