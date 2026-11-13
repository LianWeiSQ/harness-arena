'use strict';
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const statusText = {queued:'待执行',preparing:'准备中',running:'执行中',completed:'已完成',incomplete:'未完整完成',interrupted:'已中断',cancelled:'已停止',not_run:'未执行',passed:'通过',failed:'未通过',scored:'已评分',error:'执行异常'};
const initialQuery=new URLSearchParams(location.search);
let state = {datasets:[],runs:[]}, profiles = [], selected = new Set(), page = 0, currentRun = null, requestedRun = initialQuery.get('run')||'', loading = false;
let pendingTrial=initialQuery.get('trial')||'';
let evidenceSelection = null, evidenceBusy = false, evidenceInitialized = false;
let entries = [{runner:'pi',model_profile:''},{runner:'dsh',model_profile:''}];
async function api(url, body) {
  const signal=AbortSignal.timeout(url.endsWith('/preflight')?40000:15000);
  const response = await fetch(url, body === undefined ? {signal} : {signal,method:'POST', headers:{'Content-Type':'application/json','X-EvoArena-Request':'1','Idempotency-Key':crypto.randomUUID()},body:JSON.stringify(body)});
  const data = await response.json(); if (!response.ok) throw new Error(data.error || '请求失败'); return data;
}
function notice(text) { $('notice').textContent = text; $('notice').hidden = !text; }
function duration(seconds) {
  if(seconds==null||!Number.isFinite(seconds))return '—';
  const n=Math.max(0,Math.floor(seconds));return n<60?`${n} 秒`:n<3600?`${Math.floor(n/60)} 分 ${n%60} 秒`:`${Math.floor(n/3600)} 小时 ${Math.floor(n%3600/60)} 分`;
}
function architecture(progress) {
  if(!progress?.guest_arch)return '';
  const guest=progress.guest_arch.toUpperCase(),host=progress.host_arch?.toUpperCase();
  return host&&host!==guest?`${guest} 镜像 → ${host} 主机 · 模拟运行，准备可能较慢`:`${guest} 任务环境`;
}
function renderLiveProgress(run) {
  const trial=run.trials.find(t=>t.status==='running'||t.status==='cancelling');
  $('live-progress').hidden=!trial;
  if(!trial)return;
  const p=trial.progress||{},stage=p.stage||'environment';
  const index={environment:0,dependencies:1,node:1,harness:1,ready:1,agent:2,model:2,verifier:3}[stage]??0;
  const elapsed=p.elapsed_seconds??(trial.started?(Date.now()-Date.parse(trial.started))/1000:null);
  $('live-progress').innerHTML=`<div class="live-title"><b>${esc(trial.runner.toUpperCase())} · ${esc(p.label||trial.phase||'执行中')}</b><span>${duration(elapsed)}</span></div><div class="phase-track">${['任务环境','运行器安装','模型执行','验收'].map((name,i)=>`<span class="${i===index?'current':i<index?'done':''}">${i<index?'✓ ':''}${name}</span>`).join('')}</div><p class="muted">${esc(trial.task)}${architecture(p)?' · '+esc(architecture(p)):''}</p>`;
}
function setLog(id, text) {
  const element=$(id),bottom=element.scrollTop+element.clientHeight>=element.scrollHeight-20;
  if(element.textContent!==text){element.textContent=text;if(bottom)element.scrollTop=element.scrollHeight;}
}
function renderEvidence(t) {
  const p=t.progress||{},active=['running','preparing','cancelling'].includes(t.status);
  const installation=['environment','dependencies','node','harness','ready'].includes(p.stage);
  if(!evidenceInitialized){
    $('evidence-content').innerHTML=`<div class="live-title"><b id="evidence-phase"></b><span id="evidence-time"></span></div><p id="evidence-architecture" class="muted"></p><p id="evidence-message" role="status"></p><p id="evidence-freshness" class="muted"></p><details data-evidence="installation" ${installation?'open':''}><summary>安装日志</summary><pre id="installation-log"></pre></details><details data-evidence="harbor" ${installation&&!t.installation_log?'open':''}><summary>Harbor 日志</summary><pre id="harbor-log"></pre></details><details data-evidence="agent" ${active&&!installation?'open':''}><summary>工具实时输出</summary><pre id="agent-output"></pre></details><details data-evidence="verifier" ${!active&&t.status!=='queued'?'open':''}><summary>验收器输出</summary><pre id="verifier-log"></pre></details><details data-evidence="trace" ${active&&!installation?'open':''}><summary id="trace-title">执行轨迹</summary><div id="trace-content"></div></details><details data-evidence="identity"><summary>任务校验和</summary><pre id="evidence-checksum"></pre></details>`;
    evidenceInitialized=true;
  }
  $('evidence-title').textContent=t.task+' · '+t.runner.toUpperCase();
  $('evidence-phase').textContent=active?(p.label||t.phase||'执行中'):(statusText[t.status]||t.status);
  $('evidence-time').textContent='已用 '+duration(p.elapsed_seconds??t.seconds);
  $('evidence-architecture').textContent=architecture(p);
  $('evidence-architecture').hidden=!architecture(p);
  $('evidence-message').textContent=t.error||(t.status==='queued'?'本次顺序执行，等待前一组合结束。':installation&&active?'正在准备运行环境，尚未进入做题阶段。':'');
  $('evidence-freshness').textContent=active?`每 4 秒自动刷新${p.log_age_seconds!=null?' · 最近日志 '+duration(p.log_age_seconds)+'前':''}${p.stage_elapsed_seconds!=null?' · 当前步骤 '+duration(p.stage_elapsed_seconds):''}`:'执行记录已保存';
  setLog('installation-log',t.installation_log||'尚无安装输出；可查看 Harbor 准备日志。');
  setLog('harbor-log',t.harbor_log||'Harbor 尚未输出日志。');
  setLog('agent-output',t.agent_output||'尚无工具输出；模型请求和响应见执行轨迹。');
  setLog('verifier-log',t.verifier_output||(active?'Agent 完成后进入验收。':'尚无验收结果。'));
  setLog('evidence-checksum',t.task_checksum||'等待 Harbor 记录');
  $('trace-title').textContent=`执行轨迹 · ${(t.trace||[]).length}`;
  const trace=(t.trace||[]).map(row=>`<div class="trace-row"><b>${esc(row.title)}</b><pre>${esc(row.body)}</pre></div>`).join('');
  if($('trace-content').innerHTML!==trace)$('trace-content').innerHTML=trace;
}
async function refreshEvidence() {
  const selected=evidenceSelection;
  if(!selected||evidenceBusy)return;
  evidenceBusy=true;
  try{
    const t=await api('/api/benchmarks/runs/'+encodeURIComponent(selected.run)+'/trials/'+encodeURIComponent(selected.trial));
    if(evidenceSelection===selected&&$('evidence-dialog').open)renderEvidence(t);
  }catch(error){
    if(evidenceSelection===selected){
      if($('evidence-freshness'))$('evidence-freshness').textContent='详情更新失败，将自动重试：'+error.message;
      else $('evidence-content').textContent='读取失败，将自动重试：'+error.message;
    }
  }finally{evidenceBusy=false;}
}
function dataset() { return state.datasets.find(d => d.id === $('dataset').value) || state.datasets[0]; }
function route() { const view = location.hash === '#datasets' ? 'datasets' : 'evaluation'; document.querySelectorAll('[data-view]').forEach(e => e.hidden = e.dataset.view !== view); document.querySelectorAll('[data-nav]').forEach(e=>e.classList.toggle('active',e.dataset.nav===view)); }
function renderEntries() {
  $('entries').innerHTML = entries.map((e,i)=>`<div class="entry"><select data-entry="${i}" data-field="runner" aria-label="Harness ${i+1}"><option value="pi" ${e.runner==='pi'?'selected':''}>Pi</option><option value="dsh" ${e.runner==='dsh'?'selected':''}>DSH</option></select><select data-entry="${i}" data-field="model_profile" aria-label="模型 ${i+1}">${profiles.length?profiles.map(p=>`<option value="${esc(p.id)}" ${p.id===e.model_profile?'selected':''}>${esc(p.name)} · ${esc(p.model)}</option>`).join(''):'<option value="">先配置模型</option>'}</select><a class="edit-model" href="/settings?edit=${encodeURIComponent(e.model_profile)}" aria-label="编辑模型 ${i+1}">编辑</a><button data-remove="${i}" aria-label="移除组合 ${i+1}" ${entries.length===1?'disabled':''}>×</button></div>`).join('');
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
  renderLiveProgress(r);
  const done=r.trials.filter(t=>!['queued','running'].includes(t.status)).length;
  $('progress').textContent=`${done} / ${r.trials.length}`;
  $('trials').innerHTML=`<div class="table-scroll"><table><thead><tr><th>任务</th><th>组合</th><th>结果</th><th>耗时</th><th></th></tr></thead><tbody>${r.trials.map(t=>`<tr><td class="task-name">${esc(t.task)}${r.repeats>1?' #'+t.repeat:''}</td><td>${esc(t.runner.toUpperCase())}<span class="muted">${esc(t.model)}</span></td><td class="status-${esc(t.status)}">${esc(t.status==='running'?(t.progress?.label||t.phase||'执行中'):(statusText[t.status]||t.status))}${t.error?'<span class="muted">'+esc(t.error)+'</span>':''}${t.status==='scored'?'<span class="muted">'+esc(JSON.stringify(t.rewards))+'</span>':''}</td><td>${duration(t.progress?.elapsed_seconds ?? t.seconds)}</td><td><button data-trial="${esc(t.id)}" class="text-button">查看 ↗</button></td></tr>`).join('')}</tbody></table></div>`;
}
async function refresh() {
  if(loading) return; loading=true;
  try {
    state=await api('/api/benchmarks'); renderCatalog();
    const chosen=requestedRun||$('history').value||state.runs[0]?.id||'';
    $('history').innerHTML=state.runs.length?state.runs.map(r=>`<option value="${esc(r.id)}">${esc(statusText[r.status]||r.status)} · ${esc(new Date(r.created).toLocaleString())}</option>`).join(''):'<option value="">尚无评测</option>';
    if(chosen&&state.runs.some(r=>r.id===chosen)) { $('history').value=chosen;currentRun=await api('/api/benchmarks/runs/'+encodeURIComponent(chosen));renderRun(); }
    requestedRun='';
    if(pendingTrial&&currentRun){const id=pendingTrial;pendingTrial='';await openEvidence(id);}
    else if ($('evidence-dialog').open) await refreshEvidence();
  } catch(e) { notice(e.message); } finally { loading=false; }
}
$('entries').onchange=e=>{const i=e.target.dataset.entry;if(i!==undefined) {entries[Number(i)][e.target.dataset.field]=e.target.value;renderEntries();}updateCount();};
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
$('history').onchange=async()=>{requestedRun=$('history').value;window.history.replaceState(null,'','/evo?run='+encodeURIComponent(requestedRun)+location.hash);await refresh();};
$('cancel').onclick=async()=>{if(currentRun){try{await api('/api/benchmarks/runs/'+currentRun.id+'/cancel',{});notice('正在停止任务并关闭模型连接。');}catch(e){notice(e.message);}}};
$('open-import').onclick=()=>$('import-dialog').showModal();
document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>$(b.dataset.close).close());
$('import-form').onsubmit=async e=>{e.preventDefault();$('import-submit').disabled=true;try{const d=await api('/api/benchmarks/import',{name:$('import-name').value,path:$('import-path').value});$('import-dialog').close();await refresh();$('dataset').value=d.id;selected.clear();renderCatalog();notice('已导入自建评测集。');}catch(err){$('import-error').textContent=err.message;}finally{$('import-submit').disabled=false;}};
async function openEvidence(id){
  if(!currentRun?.trials.some(t=>t.id===id)){notice('这条执行记录不存在。');return;}
  evidenceSelection={run:currentRun.id,trial:id};evidenceInitialized=false;
  window.history.replaceState(null,'','/evo?'+new URLSearchParams({run:currentRun.id,trial:id})+location.hash);
  $('evidence-title').textContent='执行详情';$('evidence-content').textContent='正在读取执行进度…';
  $('evidence-dialog').showModal();await refreshEvidence();
}
$('trials').onclick=async e=>{const id=e.target.closest('[data-trial]')?.dataset.trial;if(id)await openEvidence(id);};
$('evidence-dialog').addEventListener('close',()=>{evidenceSelection=null;evidenceInitialized=false;const query=new URLSearchParams(location.search);query.delete('trial');window.history.replaceState(null,'','/evo?'+query+location.hash);});

window.addEventListener('hashchange',route);route();
(async()=>{try{const m=await api('/api/models');profiles=m.profiles;entries.forEach(e=>e.model_profile=m.default||profiles[0]?.id||'');renderEntries();await refresh();}catch(e){notice(e.message);}setInterval(refresh,4000);})();
