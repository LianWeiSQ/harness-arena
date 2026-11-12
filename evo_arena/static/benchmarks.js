'use strict';
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const statusText = {queued:'待执行',preparing:'准备中',running:'执行中',completed:'已完成',incomplete:'未完整完成',interrupted:'已中断',cancelled:'已停止',not_run:'未执行',passed:'通过',failed:'未通过',scored:'已评分',error:'执行异常'};
let state = {datasets:[],runs:[]}, profiles = [], selected = new Set(), page = 0, currentRun = null, requestedRun = '', loading = false;
let entries = [{runner:'pi',model_profile:''},{runner:'dsh',model_profile:''}];
async function api(url, body) {
  const response = await fetch(url, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json','X-EvoArena-Request':'1','Idempotency-Key':crypto.randomUUID()},body:JSON.stringify(body)});
  const data = await response.json(); if (!response.ok) throw new Error(data.error || '请求失败'); return data;
}
function notice(text) { $('notice').textContent = text; $('notice').hidden = !text; }
function dataset() { return state.datasets.find(d => d.id === $('dataset').value) || state.datasets[0]; }
function route() { const view = location.hash === '#datasets' ? 'datasets' : 'evaluation'; document.querySelectorAll('[data-view]').forEach(e => e.hidden = e.dataset.view !== view); document.querySelectorAll('[data-nav]').forEach(e=>e.classList.toggle('active',e.dataset.nav===view)); }
function renderEntries() {
  $('entries').innerHTML = entries.map((e,i)=>`<div class="entry"><select data-entry="${i}" data-field="runner" aria-label="Harness ${i+1}"><option value="pi" ${e.runner==='pi'?'selected':''}>Pi</option><option value="dsh" ${e.runner==='dsh'?'selected':''}>DSH</option></select><select data-entry="${i}" data-field="model_profile" aria-label="模型 ${i+1}">${profiles.length?profiles.map(p=>`<option value="${esc(p.id)}" ${p.id===e.model_profile?'selected':''}>${esc(p.name)} · ${esc(p.model)}</option>`).join(''):'<option value="">先配置模型</option>'}</select><button data-remove="${i}" aria-label="移除组合 ${i+1}" ${entries.length===1?'disabled':''}>×</button></div>`).join('');
  updateCount();
}
function updateCount() {
  const count = selected.size * entries.length * Number($('repeats').value);
  $('selected-count').textContent = selected.size; $('trial-count').textContent = Number.isFinite(count)?count:0;
  $('start').disabled = !selected.size || !profiles.length || dataset()?.status !== 'ready' || state.runs.some(r=>['queued','running','preparing'].includes(r.status)) || state.installation?.status==='running';
}
function renderCatalog() {
  const old = $('dataset').value;
  $('dataset').innerHTML = state.datasets.map(d=>`<option value="${esc(d.id)}">${esc(d.name)}</option>`).join('');
  if (state.datasets.some(d=>d.id===old)) $('dataset').value=old;
  const d = dataset(); if(!d) return;
  $('dataset-name').textContent=d.name;
  $('dataset-state').textContent=d.status==='ready'?'已接入':'待接入';
  $('dataset-meta').textContent=`${d.tasks.length||d.expected_tasks||0} 个任务${d.revision?' · '+d.revision.slice(0,12):' · 内容快照 '+(d.sha256||'').slice(0,12)}`;
  $('sync').hidden=d.kind!=='official'; $('sync').disabled=state.installation?.status==='running';
  $('sync').textContent=state.installation?.status==='running'?'正在接入…':d.status==='ready'?'核验任务集':'接入官方任务集 ↓';
  $('source-info').textContent=d.kind==='official'?'官方任务 · 保留原始环境与验收脚本':'自建任务 · 导入快照';
  const oldCategory=$('category').value;
  $('category').innerHTML='<option value="">全部分类</option>'+[...new Set(d.tasks.map(t=>t.category))].sort().map(c=>`<option value="${esc(c)}">${esc(c)}</option>`).join('');
  if([...$('category').options].some(o=>o.value===oldCategory)) $('category').value=oldCategory;
  if(state.installation?.status==='error') notice(state.installation.error);
  renderTasks(); updateCount();
}
function filteredTasks() { const q=$('search').value.toLowerCase(),c=$('category').value;return (dataset()?.tasks||[]).filter(t=>(!c||t.category===c)&&(!q||(t.id+' '+t.description).toLowerCase().includes(q))); }
function renderTasks() {
  const tasks=filteredTasks(), pages=Math.max(1,Math.ceil(tasks.length/10)); page=Math.min(page,pages-1);
  $('task-list').innerHTML=tasks.length?`<div class="table-scroll"><table><thead><tr><th></th><th>任务</th><th>分类</th><th>时限 / 资源</th></tr></thead><tbody>${tasks.slice(page*10,page*10+10).map(t=>`<tr><td><input class="task-select" type="checkbox" data-task="${esc(t.id)}" aria-label="选择 ${esc(t.id)}" ${selected.has(t.id)?'checked':''}></td><td><span class="task-name">${esc(t.id)}</span><span class="muted">${esc(t.description)}</span></td><td>${esc(t.category)}<span class="muted">${esc(t.difficulty)}</span></td><td>${Math.round(t.seconds/60)} 分钟<span class="muted">${esc(t.cpus)} CPU · ${Math.round((t.memory_mb||0)/1024*10)/10} GB${t.gpus?' · '+t.gpus+' GPU':''}</span></td></tr>`).join('')}</tbody></table></div>`:'<div class="empty"><h3>暂无任务</h3><p>接入官方任务集，或导入自己的 Harbor 目录。</p></div>';
  $('task-count').textContent=`${tasks.length} 个任务 · 已选 ${selected.size}`; $('page-number').textContent=`${page+1} / ${pages}`;
  $('prev').disabled=page===0; $('next').disabled=page>=pages-1;
}
function renderRun() {
  if(!currentRun) return;
  const r=currentRun;
  $('run-meta').textContent=`${r.dataset.name} · ${statusText[r.status]||r.status} · ${r.selected_tasks.length} 题 × ${r.entries.length} 组合 × ${r.repeats} 次${r.error?' · '+r.error:''}`;
  $('cancel').hidden=!['queued','running','preparing'].includes(r.status);
  $('comparison').innerHTML=`<div class="comparison-grid">${r.summary.map(e=>`<div class="score-card"><b>${esc(e.runner.toUpperCase())}</b><div class="model">${esc(e.model)}</div><div class="score">${e.pass_rate===null?'—':(e.pass_rate*100).toFixed(1)+'%'}<small>${e.pass_rate===null?'尚无完整通过率':'通过率'}</small></div><progress class="bar" max="${Math.max(1,e.total)}" value="${e.passed}" aria-label="已通过任务比例"></progress><div class="score-meta">${e.passed} 通过 · ${e.failed} 未通过 · ${e.error} 异常<br>${e.calls} 请求 · ${e.tokens.toLocaleString()} Token${e.unknown_usage_calls?' · '+e.unknown_usage_calls+' 次用量未知':''}${e.unconfirmed_usage_trials?' · '+e.unconfirmed_usage_trials+' 题用量待核对':''}</div></div>`).join('')}</div>`;
  const done=r.trials.filter(t=>!['queued','running'].includes(t.status)).length;
  $('progress').textContent=`${done} / ${r.trials.length}`;
  $('trials').innerHTML=`<div class="table-scroll"><table><thead><tr><th>任务</th><th>组合</th><th>结果</th><th>耗时</th><th></th></tr></thead><tbody>${r.trials.map(t=>`<tr><td class="task-name">${esc(t.task)}${r.repeats>1?' #'+t.repeat:''}</td><td>${esc(t.runner.toUpperCase())}<span class="muted">${esc(t.model)}</span></td><td class="status-${esc(t.status)}">${esc(t.status==='running'?(t.phase||'执行中'):(statusText[t.status]||t.status))}${t.status==='scored'?'<span class="muted">'+esc(JSON.stringify(t.rewards))+'</span>':''}</td><td>${t.seconds===undefined?'—':Math.round(t.seconds)+'s'}</td><td><button data-trial="${esc(t.id)}" class="text-button">查看 ↗</button></td></tr>`).join('')}</tbody></table></div>`;
}
async function refresh() {
  if(loading) return; loading=true;
  try {
    state=await api('/api/benchmarks'); renderCatalog();
    const chosen=requestedRun||$('history').value||state.runs[0]?.id||'';
    $('history').innerHTML=state.runs.length?state.runs.map(r=>`<option value="${esc(r.id)}">${esc(statusText[r.status]||r.status)} · ${esc(new Date(r.created).toLocaleString())}</option>`).join(''):'<option value="">尚无评测</option>';
    if(chosen&&state.runs.some(r=>r.id===chosen)) { $('history').value=chosen;currentRun=await api('/api/benchmarks/runs/'+encodeURIComponent(chosen));renderRun(); }
    requestedRun='';
  } catch(e) { notice(e.message); } finally { loading=false; }
}
$('entries').onchange=e=>{const i=e.target.dataset.entry;if(i!==undefined) entries[Number(i)][e.target.dataset.field]=e.target.value;updateCount();};
$('entries').onclick=e=>{const i=e.target.dataset.remove;if(i!==undefined&&entries.length>1){entries.splice(Number(i),1);renderEntries();}};
$('add-entry').onclick=()=>{if(entries.length<8){entries.push({runner:'pi',model_profile:profiles[0]?.id||''});renderEntries();}};
$('dataset').onchange=()=>{selected.clear();page=0;renderCatalog();};
$('task-list').onchange=e=>{const task=e.target.dataset.task;if(task){if(e.target.checked)selected.add(task);else selected.delete(task);updateCount();$('task-count').textContent=`${filteredTasks().length} 个任务 · 已选 ${selected.size}`;}};
for(const id of ['search','category']) $(id).addEventListener('input',()=>{page=0;renderTasks();});
$('prev').onclick=()=>{page--;renderTasks();};$('next').onclick=()=>{page++;renderTasks();};
$('select-filtered').onclick=()=>{filteredTasks().forEach(t=>selected.add(t.id));renderTasks();updateCount();};
$('clear-selection').onclick=()=>{selected.clear();renderTasks();updateCount();};
$('repeats').oninput=updateCount;
$('sync').onclick=async()=>{try{$('sync').disabled=true;await api('/api/benchmarks/sync',{});notice('正在下载并校验官方任务，不调用模型。');await refresh();}catch(e){notice(e.message);$('sync').disabled=false;}};
$('preflight').onclick=async()=>{try{$('preflight').disabled=true;const r=await api('/api/benchmarks/preflight',{});notice(r.checks.map(c=>`${c.ok?'✓':'×'} ${c.name}：${c.detail}`).join('　'));}catch(e){notice(e.message);}finally{$('preflight').disabled=false;}};
$('start').onclick=async()=>{try{$('start').disabled=true;const run=await api('/api/benchmarks/runs',{dataset:dataset().id,tasks:[...selected],entries,repeats:Number($('repeats').value),max_calls:Number($('calls').value),max_tokens:Number($('tokens').value)});requestedRun=run.id;notice('评测已提交。');await refresh();}catch(e){notice(e.message);updateCount();}};
$('history').onchange=async()=>{requestedRun=$('history').value;await refresh();};
$('cancel').onclick=async()=>{if(currentRun){try{await api('/api/benchmarks/runs/'+currentRun.id+'/cancel',{});notice('正在停止，已发出的模型请求会结算。');}catch(e){notice(e.message);}}};
$('open-import').onclick=()=>$('import-dialog').showModal();
document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>$(b.dataset.close).close());
$('import-form').onsubmit=async e=>{e.preventDefault();$('import-submit').disabled=true;try{const d=await api('/api/benchmarks/import',{name:$('import-name').value,path:$('import-path').value});$('import-dialog').close();await refresh();$('dataset').value=d.id;selected.clear();renderCatalog();notice('已导入自建评测集。');}catch(err){$('import-error').textContent=err.message;}finally{$('import-submit').disabled=false;}};
$('trials').onclick=async e=>{const id=e.target.closest('[data-trial]')?.dataset.trial;if(!id)return;let t;try{t=await api('/api/benchmarks/runs/'+encodeURIComponent(currentRun.id)+'/trials/'+encodeURIComponent(id));}catch(error){notice(error.message);return;}$('evidence-title').textContent=t.task+' · '+t.runner.toUpperCase();$('evidence-content').innerHTML=`<p>${esc(statusText[t.status]||t.status)}${t.error?' · '+esc(t.error):''}</p><p class="muted">校验和 ${esc(t.task_checksum||'等待 Harbor 记录')}</p><details open><summary>验收器输出</summary><pre>${esc(t.verifier_output||'尚无验收结果')}</pre></details><details><summary>Harbor 日志</summary><pre>${esc(t.harbor_log||'尚无日志')}</pre></details><details><summary>执行轨迹 · ${(t.trace||[]).length}</summary>${(t.trace||[]).map(row=>`<div class="trace-row"><b>${esc(row.title)}</b><pre>${esc(row.body)}</pre></div>`).join('')}</details>`;$('evidence-dialog').showModal();};
window.addEventListener('hashchange',route);route();
(async()=>{try{const m=await api('/api/models');profiles=m.profiles;entries.forEach(e=>e.model_profile=m.default||profiles[0]?.id||'');renderEntries();await refresh();}catch(e){notice(e.message);}setInterval(refresh,4000);})();
