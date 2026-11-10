const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {overview:null, experiment:null, busy:false, selected:null};
const labels = {queued:'等待执行',preparing:'准备环境',running:'执行中',completed:'评测完成',incomplete:'执行未完成',interrupted:'执行已中断',passed:'业务验收通过',failed:'业务验收未通过',error:'执行异常'};
const outcomes={improved:'观察到提升',unchanged:'未观察到收益',regression:'发生退化或违反关键约束',insufficient_evidence:'证据不足'};
const families={receipt:'下单结果核对',listing:'完整订单对账',workflow:'订单履约与补偿'};
const active = status => ['queued','preparing','running'].includes(status);
const number = value => typeof value === 'number' ? value.toLocaleString('zh-CN') : '—';
async function api(path, payload, key) {
  const options = payload ? {method:'POST',headers:{'Content-Type':'application/json','X-EvoArena-Request':'1',...(key?{'Idempotency-Key':key}:{})},body:JSON.stringify(payload)} : {};
  const response = await fetch(path, options), result = await response.json();
  if (!response.ok) throw new Error(result.error || '请求失败');
  return result;
}
function notice(message) {$('notice').hidden=!message;$('notice').textContent=message;}
function options(select, rows, value) {select.innerHTML=rows.map(([id,label])=>`<option value="${esc(id)}">${esc(label)}</option>`).join('');if(rows.some(([id])=>id===value))select.value=value;}
function version(id) {return state.overview.versions.find(row=>row.id===id);}
function populateCandidates() {
  const candidates=state.overview.versions.filter(row=>row.parent===$('baseline').value);
  options($('candidate'),candidates.length?candidates.map(row=>[row.id,row.name]):[['','先从基线创建候选']],$('candidate').value);
  $('run-experiment').disabled=state.busy||!state.overview.ready||!$('candidate').value||state.overview.experiments.some(row=>active(row.status));
  previewSelection();
}
let previewKey='';
async function previewSelection(){
  const baseline=$('baseline').value,candidate=$('candidate').value,key=baseline+':'+candidate;
  if(key===previewKey)return;previewKey=key;
  if(!candidate){$('draft-diff').textContent='请选择一组基线与候选。';return;}
  try{const result=await api('/api/evo/diff?'+new URLSearchParams({baseline,candidate}));if(previewKey===key)$('draft-diff').textContent=result.diff;}
  catch(error){if(previewKey===key)$('draft-diff').textContent=error.message;}
}
function renderOverview() {
  const data=state.overview;
  $('version-count').textContent=data.versions.length;$('experiment-count').textContent=data.experiments.length;
  options($('baseline'),data.versions.length?data.versions.map(row=>[row.id,row.name]):[['','请先创建基线']],$('baseline').value);
  populateCandidates();
  options($('history'),data.experiments.length?data.experiments.map(row=>[row.id,`${row.id.slice(-6)} · ${labels[row.status]||row.status}`]):[['','暂无实验']],state.selected);
  $('version-list').innerHTML=data.versions.length?data.versions.map(row=>`<article class="evo-version"><h3>${esc(row.name)}</h3><p>${esc(data.capabilities[row.manifest.runner].name)} · ${esc(row.manifest.model.model)}</p><p>${Object.keys(row.manifest.files).length-1} 个 Skill · ${row.parent?'候选版本':'初始基线'}</p><code>${esc(row.manifest_sha256.slice(0,16))}</code><br><button data-parent="${esc(row.id)}">从此版本创建候选 ↗</button></article>`).join(''):'<div class="evo-empty"><p>还没有版本。先添加模型连接，再创建一个基线。</p><a href="/settings">配置模型 →</a></div>';
  $('capabilities').innerHTML=Object.entries(data.capabilities).map(([id,c])=>`<div class="evo-capability"><strong>${esc(c.name)} <span class="evo-muted">${esc(c.runtime_version)}</span></strong><p>${id==='pi'?'系统提示追加 · 原生 Skill 文件':'任务指令前缀 · 原生 Skill 未验证'}</p></div>`).join('');
  $('new-version').disabled=!data.models.profiles.length;
  if(!data.ready)notice('运行实验需要可用的模型连接和 Harbor CLI。已有版本与记录仍可查看。');
}
function renderExperiment() {
  const ex=state.experiment;if(!ex)return;
  $('version-diff').textContent=ex.diff||'没有文件差异。';
  const cards=['baseline','candidate'].map(role=>{
    const row=version(ex[role]),runs=ex.runs.filter(r=>r.role===role),run=runs.at(-1),input=run?.input_evidence;
    const skills=row?Object.keys(row.manifest.files).length-1:0;
    if(ex.plan){
      const planned=ex.plan.trials.filter(r=>r.role===role).length,done=runs.filter(r=>['passed','failed'].includes(r.status)),unknown=runs.some(r=>r.usage?.unknown_usage_calls);
      return `<article class="evo-side"><span class="evo-role">${role==='baseline'?'BASELINE / 基线':'CANDIDATE / 候选'}</span><h3>${esc(row?.name||ex[role])}</h3><p>${esc(ex.runner)} · ${esc(ex.model)}</p><div class="evo-metrics"><span>通过 / 计划<b>${done.filter(r=>r.status==='passed').length}/${planned}</b></span><span>有效完成<b>${done.length}/${planned}</b></span><span>已知 Token<b>${unknown?'未知':number(runs.reduce((n,r)=>n+(r.usage?.tokens||0),0))}</b></span></div><ul class="evo-facts"><li>${runs.filter(r=>r.version_load?.version_id&&r.input_evidence?.registration_verified).length}/${planned} 次加载证据完整</li><li>${runs.filter(r=>r.status==='error'||r.status==='interrupted').length} 次执行异常，不折算为答题失败</li><li>每次独立环境 · 同题同预算配对</li></ul>${run?.error?`<p class="evo-error">${esc(run.error)}</p>`:''}</article>`;
    }
    return `<article class="evo-side"><span class="evo-role">${role==='baseline'?'BASELINE / 基线':'CANDIDATE / 候选'}</span><h3>${esc(row?.name||ex[role])}</h3><p>${esc(ex.runner)} · ${esc(ex.model)}</p><p>${esc(labels[run?.status]||'尚未运行')}</p><div class="evo-metrics"><span>验收<b>${run?.checks?.length?`${run.checks.filter(c=>c.passed).length}/${run.checks.length}`:'—'}</b></span><span>耗时<b>${run?.seconds!=null?`${run.seconds}s`:'—'}</b></span><span>Token<b>${run?.usage?.unknown_usage_calls?"未知":number(run?.usage?.tokens)}</b></span></div><ul class="evo-facts"><li class="${run?.version_load?.version_id?'evo-ok':''}">${run?.version_load?.version_id?'✓':'·'} 挂载内容与版本哈希核对</li><li class="${input?.registration_verified?'evo-ok':''}">${input?.registration_verified?'✓':'·'} 策略与 Skill 注册进入模型输入</li><li>${skills?`${input?.skill_contents_in_input?.length||0}/${skills} 个 Skill 正文已进入模型上下文`:'此版本未配置 Skill'}</li></ul>${run?.error?`<p class="evo-error">${esc(run.error)}</p>`:''}</article>`;
  }).join('');
  const evaluation=ex.evaluation,budget=ex.budget;
  const rows=ex.plan?[...new Set(ex.plan.trials.map(r=>r.task))].map(task=>{const spec=ex.plan.trials.find(r=>r.task===task),g=evaluation?.per_case?.[task];return `<tr><td>${esc(families[spec.family])}<br><small>${esc(task)}</small></td><td>${g?`${g.baseline}/${ex.plan.repeats}`:'—'}</td><td>${g?`${g.candidate}/${ex.plan.repeats}`:'—'}</td></tr>`;}).join(''):'';
  const scope=ex.plan?`${state.overview.protocols[ex.mode]?.label||ex.mode} · ${ex.mode==='validation'?'满足预设门槛也需后续准入；不声明统计显著性。':ex.mode==='report'?'封存结果仅用于最终报告，不回流候选生成。':'仅开发冒烟，不作为泛化或晋升证据。'}`:'当前仅验证加载与单任务执行，不判断泛化或晋升。';
  $('experiment-body').innerHTML=`<div class="evo-result-head"><strong>${esc(labels[ex.status]||ex.status)}${evaluation&&!active(ex.status)?' · '+esc(outcomes[evaluation.outcome]):''}</strong><span>${esc(scope)}</span></div>${ex.error?`<p class="evo-error evo-scope">${esc(ex.error)}</p>`:''}<div class="evo-pair">${cards}</div>${ex.plan?`<div class="evo-coverage"><table><thead><tr><th>场景 / 每个版本通过次数</th><th>基线</th><th>候选</th></tr></thead><tbody>${rows}</tbody></table><p>${evaluation?`${evaluation.valid_trials}/${evaluation.planned_trials} 次有效结果 · 候选关键约束违反 ${evaluation.critical_violations} 次`:'等待配对结果。'} · 未执行项不算失败</p></div>`:''}${budget?`<div class="evo-budget"><span>总请求 ${budget.calls}/${budget.limits.calls}</span><span>已知 Token ${number(budget.tokens)}/${number(budget.limits.tokens)}</span><span>待结算 ${budget.pending} · 用量未知 ${budget.unknown_usage_calls}</span><span>总时限 ${budget.limits.seconds}s</span></div>`:''}`;
  options($('run-select'),ex.runs.length?ex.runs.map(r=>[r.id,`${r.role==='baseline'?'基线':'候选'} · ${r.family?families[r.family]+' 场景 '+r.task.split('-').at(-1)+' · 第 '+(r.repeat+1)+' 次 · ':''}${labels[r.status]||r.status}`]):[['','暂无执行']],$('run-select').value);
  renderEvidence();
}
function renderEvidence(){
  const run=state.experiment?.runs.find(r=>r.id===$('run-select').value);if(!run)return;
  $('run-evidence').innerHTML=`<div>${(run.checks||[]).map(c=>`<p class="${c.passed?'evo-ok':'evo-error'}">${c.passed?'✓':'×'} ${esc(c.label||c.name||c.id||JSON.stringify(c))}</p>`).join('')||'<p class="evo-muted">尚未取得业务验收结果。</p>'}</div><details><summary>版本加载、模型输入与独立验收证据</summary><pre>${esc(JSON.stringify({mounted:run.version_load||null,model_input:run.input_evidence||null,verifier:{mode:run.verifier_mode||null,attested:run.verifier_attested??null,execution_frozen:run.execution_frozen??null,task_checksum:run.task_checksum||null}},null,2))}</pre></details><details open><summary>工具轨迹 · ${(run.trace||[]).length} 条</summary>${(run.trace||[]).map(event=>`<div class="evo-trace"><b>${esc(event.label)}</b><p>${esc(event.text)}</p></div>`).join('')}</details>`;
}
async function refresh(){
  try{
    const overview=await api('/api/evo'),overviewKey=JSON.stringify(overview);
    state.overview=overview;
    if(!state.selected&&state.overview.experiments.length)state.selected=state.overview.experiments[0].id;
    if(state.overviewKey!==overviewKey){renderOverview();state.overviewKey=overviewKey;}
    if(state.selected){const experiment=await api('/api/evo/experiments/'+encodeURIComponent(state.selected)),experimentKey=JSON.stringify(experiment);
      state.experiment=experiment;
      if(state.loadedId!==state.selected){$('baseline').value=state.experiment.baseline;populateCandidates();$('candidate').value=state.experiment.candidate;previewSelection();state.loadedId=state.selected;}
      if(state.experimentKey!==experimentKey){renderExperiment();state.experimentKey=experimentKey;}}
  }catch(error){notice(error.message);}
}
function addSkill(skill={name:'',description:'',body:''}){
  if($('skills').children.length>=4)return;
  const field=document.createElement('div');field.className='evo-skill-editor';
  field.innerHTML=`<button type="button" class="remove-skill">移除</button><label>Skill 名称<input class="skill-name" required pattern="[a-z][a-z0-9-]{0,47}" maxlength="48" placeholder="例如 order-recovery" value="${esc(skill.name)}"></label><label>使用条件与描述<input class="skill-description" required maxlength="600" value="${esc(skill.description)}" placeholder="何时应该使用这个 Skill"></label><label>操作流程<textarea class="skill-body" rows="5" required maxlength="12000" placeholder="写下可复用的步骤、检查与恢复方法。">${esc(skill.body)}</textarea></label>`;
  field.querySelector('.remove-skill').onclick=()=>field.remove();$('skills').append(field);
}
function runnerChanged(){const native=$('runner').value==='pi';$('add-skill').disabled=!native;$('skill-hint').textContent=native?'Pi 使用原生 Skill 加载；正文是否进入上下文会单独记录。':'DSH 当前支持任务指令前缀，原生 Skill 加载尚未验证。';if(!native)$('skills').replaceChildren();}
function openVersion(parentId){
  $('version-form').reset();$('skills').replaceChildren();$('form-error').hidden=true;
  const parent=parentId?version(parentId):null;
  $('parent-id').value=parent?.id||'';$('form-title').textContent=parent?'从基线创建候选':'创建基线版本';
  options($('model-profile'),state.overview.models.profiles.map(row=>[row.id,`${row.name} · ${row.model}`]),parent?.manifest.model.profile||state.overview.models.default);
  $('runner').value=parent?.manifest.runner||'pi';$('runner').disabled=!!parent;$('model-profile').disabled=!!parent;
  $('version-name').value=parent?parent.name+' · 改进':'';$('policy').value=parent?.manifest.files['policy.md']||'';
  $('form-context').textContent=parent?'候选沿用父版本的模型和运行器。修改策略或 Skill 后保存一个新版本。':'模型和运行器会随版本固化，后续编辑连接不会自动改变此版本。';
  runnerChanged();
  for(const [path,content] of Object.entries(parent?.manifest.files||{}))if(path!=='policy.md'){
    const parts=content.split('\n---\n');const line=parts[0].split('\n').find(row=>row.startsWith('description: '));
    addSkill({name:path.split('/')[1],description:line?JSON.parse(line.slice(13)):'',body:parts.slice(1).join('\n---\n').replace(/^\n/,'').replace(/\n$/,'')});
  }
  $('version-dialog').showModal();
}
$('new-version').onclick=()=>openVersion();$('close-dialog').onclick=()=>$('version-dialog').close();$('add-skill').onclick=()=>addSkill();$('runner').onchange=runnerChanged;
$('version-list').onclick=event=>{const button=event.target.closest('[data-parent]');if(button)openVersion(button.dataset.parent);};
$('baseline').onchange=populateCandidates;$('candidate').onchange=previewSelection;$('run-select').onchange=renderEvidence;
$('history').onchange=()=>{state.selected=$('history').value;refresh();};
$('version-form').onsubmit=async event=>{
  event.preventDefault();$('save-version').disabled=true;$('form-error').hidden=true;
  try{
    const skills=[...$('skills').children].map(row=>({name:row.querySelector('.skill-name').value,description:row.querySelector('.skill-description').value,body:row.querySelector('.skill-body').value}));
    const result=await api('/api/evo/versions',{name:$('version-name').value,runner:$('runner').value,model_profile:$('model-profile').value,parent:$('parent-id').value||null,policy:$('policy').value,skills});
    $('version-dialog').close();notice('版本已保存。内容和模型身份已固化。');await refresh();
    $('baseline').value=result.parent||result.id;populateCandidates();if(result.parent)$('candidate').value=result.id;
  }catch(error){$('form-error').textContent=error.message;$('form-error').hidden=false;}finally{$('save-version').disabled=false;}
};
let submitKey=null;
function protocolChanged(){
  const mode=$('protocol').value,config=state.overview?.protocols?.[mode]||{limits:{calls:24,tokens:360000,seconds:900}};
  for(const key of ['calls','tokens','seconds'])$('budget-'+key).value=config.limits[key];
  $('protocol-hint').textContent=mode==='smoke'?'3 类业务问题，每类抽取 1 道开发题。用于确认执行链路，不产生晋升结论。每题最多 12 次模型请求。':mode==='validation'?'6 道验证题，每题重复 2 次，交替执行版本。完整覆盖、单题不退化、总通过数提升且关键约束零违反，才满足质量门槛。':mode==='report'?'封存数据用于最终报告。结果不参与候选生成和选优；重复运行封存集会削弱其独立性。':'旧版单题加载检查。使用修正后的无副作用帮助命令，不改写已有历史记录。';
}
$('protocol').onchange=protocolChanged;
$('run-experiment').onclick=async()=>{
  if(state.busy)return;state.busy=true;populateCandidates();
  const payload={baseline:$('baseline').value,candidate:$('candidate').value,mode:$('protocol').value,limits:Object.fromEntries(['calls','tokens','seconds'].map(k=>[k,Number($('budget-'+k).value)]))};
  const signature=JSON.stringify(payload);if(!submitKey||submitKey.signature!==signature)submitKey={signature,key:crypto.randomUUID()};
  try{const result=await api('/api/evo/experiments',payload,submitKey.key);state.selected=result.id;submitKey=null;notice('实验已提交。关闭页面不会取消执行。');await refresh();}catch(error){notice(error.message);}finally{state.busy=false;populateCandidates();}
};
refresh();setInterval(()=>{if(!$('version-dialog').open&&!state.busy)refresh();},4000);
