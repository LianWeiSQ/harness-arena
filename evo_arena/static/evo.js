const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {overview:null, experiment:null, busy:false, selected:null};
const labels = {queued:'等待执行',preparing:'准备环境',running:'执行中',completed:'评测完成',incomplete:'执行未完成',interrupted:'执行已中断',cancelled:'已取消',passed:'业务验收通过',failed:'业务验收未通过',error:'执行异常'};
const outcomes={same_version:'保留初始版本 · 独立重复记录',improved:'观察到提升',unchanged:'未观察到收益',regression:'发生退化或违反关键约束',insufficient_evidence:'证据不足'};
const families={receipt:'下单结果核对',listing:'完整订单对账',workflow:'订单履约与补偿'};
const phases={baseline:'运行开发基线',diagnosing:'诊断开发证据',proposing:'生成候选文件',validating:'配对验证',decided:'判定已记录'};
const decisions={accepted:'接受候选',rejected:'拒绝候选',insufficient_evidence:'证据不足',not_applied:'推荐版本已变化，未应用'};
const reasons={improved:'完整验证集满足预设质量门槛',unchanged:'没有严格增加任务成功数',regression:'存在逐题退化或关键约束违反',incomplete_coverage:'有效结果未完整覆盖计划',project_changed:'项目版本或修订号已变化',budget_exhausted:'预算耗尽或用量未结算',invalid_candidate:'诊断或候选未通过结构与权限校验',provider_error:'优化模型调用异常',execution_error:'执行环境或运行器异常',protocol_changed:'评测协议发生变化'};
reasons.invalid_diagnosis='诊断在结构修正后仍未通过证据校验';
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
function descendsFrom(row, ancestor) {const seen=new Set();while(row&&!seen.has(row.id)){if(row.id===ancestor)return true;seen.add(row.id);row=version(row.parent);}return false;}
function protocolLabel(ex){return ex.feedback_mode==='none'?'无执行反馈 · 对照实验':state.overview.protocols[ex.mode]?.label||'版本加载检查';}
function populateCandidates() {
  const auto=$('protocol').value==='improve',parent=version($('baseline').value);
  const candidates=state.overview.versions.filter(row=>$('protocol').value==='report'?descendsFrom(row,$('baseline').value):row.parent===$('baseline').value);
  options($('candidate'),auto?[['','运行开发集后由模型生成']]:candidates.length?candidates.map(row=>[row.id,row.name]):[['','先从基线创建候选']],$('candidate').value);
  $('candidate').disabled=auto;$('auto-config').hidden=!auto;
  options($('target'),Object.keys(parent?.manifest.files||{}).map(path=>[path,path==='policy.md'?'执行策略 · policy.md':'Skill · '+path.split('/')[1]]),$('target').value);
  options($('optimizer-profile'),state.overview.models.profiles.map(p=>[p.id,`${p.name} · ${p.model}`]),$('optimizer-profile').value||parent?.manifest.model.profile);
  $('run-experiment').textContent=auto?'开始改进实验':'验证两个版本';
  $('run-experiment').disabled=state.busy||!state.overview.ready||(auto?!parent:!$('candidate').value)||state.overview.experiments.some(row=>active(row.status));
  previewSelection();
}
let previewKey='';
async function previewSelection(){
  if($('protocol').value==='improve'){previewKey='';$('draft-diff').textContent=$('feedback-mode').value==='none'?'对照实验仍运行开发集，但生成器只看公开任务目标与初始文件；执行结果、轨迹和用量不会进入生成输入。独立记录推荐版本。':'自动改进先收集开发集反馈。候选生成后显示实际 diff；模型与运行器保持固定。';return;}
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
  renderLibrary();renderHome();renderProjects();
  $('capabilities').innerHTML=Object.entries(data.capabilities).map(([id,c])=>`<div class="evo-capability"><strong>${esc(c.name)} <span class="evo-muted">${esc(c.runtime_version)}</span></strong><p>${id==='pi'?'系统提示追加 · 原生 Skill 文件':'任务指令前缀 · 原生 Skill 未验证'}</p></div>`).join('');
  $('new-version').disabled=!data.models.profiles.length;
  $('open-experiment').textContent=!data.models.profiles.length?'配置模型连接 ↗':!data.versions.length?'创建第一个版本 ＋':'发起改进 ↗';
  if(!state.protocolInitialized){protocolChanged();state.protocolInitialized=true;}
  if(!data.ready)notice('运行实验需要可用的模型连接和 Harbor CLI。已有版本与记录仍可查看。');
}
function renderExperiment() {
  const ex=state.experiment;if(!ex)return;
  $('export-report').hidden=false;$('export-report').href=`/api/evo/experiments/${encodeURIComponent(ex.id)}/report`;
  $('experiment-title').textContent=`${ex.id.slice(-6)} · ${protocolLabel(ex)}`;
  $('version-diff').textContent=ex.diff||'没有文件差异。';
  const cards=['baseline','candidate'].map(role=>{
    const row=version(ex[role]),runs=ex.runs.filter(r=>r.role===role&&(!ex.automatic_evolution||r.phase==='validation')),run=runs.at(-1),input=run?.input_evidence;
    const skills=row?Object.keys(row.manifest.files).length-1:0;
    if(ex.plan){
      const planned=ex.plan.trials.filter(r=>r.role===role).length,done=runs.filter(r=>['passed','failed'].includes(r.status)&&!ex.evaluation?.missing?.includes(r.key)),unknown=runs.some(r=>r.usage?.unknown_usage_calls);
      return `<article class="evo-side"><span class="evo-role">${role==='baseline'?'BASELINE / 基线':'CANDIDATE / 候选'}</span><h3>${esc(row?.name||ex[role]||'等待模型生成')}</h3><p>${esc(ex.runner)} · ${esc(ex.model)}</p><div class="evo-metrics"><span>${ex.automatic_evolution?'验证通过':'通过'} / 计划<b>${done.filter(r=>r.status==='passed').length}/${planned}</b></span><span>有效完成<b>${done.length}/${planned}</b></span><span>已知 Token<b>${number(runs.reduce((n,r)=>n+(r.usage?.tokens||0),0))}${unknown?' + 未知':''}</b></span></div><ul class="evo-facts"><li>${runs.filter(r=>r.version_load?.version_id&&r.input_evidence?.registration_verified).length}/${planned} 次加载证据完整</li><li>${runs.filter(r=>r.status==='error'||r.status==='interrupted').length} 次执行异常，不折算为答题失败</li><li>每次独立环境 · 同题同预算配对</li></ul>${run?.error?`<p class="evo-error">${esc(run.error)}</p>`:''}</article>`;
    }
    return `<article class="evo-side"><span class="evo-role">${role==='baseline'?'BASELINE / 基线':'CANDIDATE / 候选'}</span><h3>${esc(row?.name||ex[role])}</h3><p>${esc(ex.runner)} · ${esc(ex.model)}</p><p>${esc(labels[run?.status]||'尚未运行')}</p><div class="evo-metrics"><span>验收<b>${run?.checks?.length?`${run.checks.filter(c=>c.passed).length}/${run.checks.length}`:'—'}</b></span><span>耗时<b>${run?.seconds!=null?`${run.seconds}s`:'—'}</b></span><span>Token<b>${run?.usage?.unknown_usage_calls?"未知":number(run?.usage?.tokens)}</b></span></div><ul class="evo-facts"><li class="${run?.version_load?.version_id?'evo-ok':''}">${run?.version_load?.version_id?'✓':'·'} 挂载内容与版本哈希核对</li><li class="${input?.registration_verified?'evo-ok':''}">${input?.registration_verified?'✓':'·'} 策略与 Skill 注册进入模型输入</li><li>${skills?`${input?.skill_contents_in_input?.length||0}/${skills} 个 Skill 正文已进入模型上下文`:'此版本未配置 Skill'}</li></ul>${run?.error?`<p class="evo-error">${esc(run.error)}</p>`:''}</article>`;
  }).join('');
  const evaluation=ex.evaluation,budget=ex.budget;
  const rows=ex.plan?[...new Set(ex.plan.trials.map(r=>r.task))].map(task=>{const spec=ex.plan.trials.find(r=>r.task===task),g=evaluation?.per_case?.[task],complete=g&&g.baseline_completed===ex.plan.repeats&&g.candidate_completed===ex.plan.repeats,delta=complete?g.candidate-g.baseline:null,run=ex.runs.findLast(r=>r.task===task&&r.role==='candidate')||ex.runs.findLast(r=>r.task===task);return `<tr><td>${esc(families[spec.family])}<br><small>${esc(task)}</small></td><td>${g?`${g.baseline}/${ex.plan.repeats}`:'—'}</td><td>${g?`${g.candidate}/${ex.plan.repeats}`:'—'}</td><td class="${delta>0?'evo-ok':delta<0?'evo-error':''}">${delta===null?'未齐':delta>0?'＋'+delta:delta<0?String(delta):'持平'}</td><td>${run?`<button data-trial="${esc(run.id)}">查看</button>`:'—'}</td></tr>`;}).join(''):'';
  const scope=ex.automatic_evolution?(ex.feedback_mode==='none'?'无执行反馈对照：生成器只读取公开契约与初始文件；开发执行仍计费，验证预算与反馈实验相同。独立项目保存推荐。':'开发反馈生成候选；验证集 6 题 × 2 次 × 双版本。封存集未参与。'):ex.plan?`${state.overview.protocols[ex.mode]?.label||ex.mode} · ${ex.mode==='validation'?'满足预设门槛也需后续准入；不声明统计显著性。':ex.mode==='report'?'封存结果仅用于最终报告，不回流候选生成。':'仅开发冒烟，不作为泛化或晋升证据。'}`:'当前仅验证加载与单任务执行，不判断泛化或晋升。';
  const development=ex.runs.filter(r=>r.phase==='development'),decision=ex.decision;
  const progress=ex.automatic_evolution?`<div class="evo-steps">${Object.entries(phases).map(([phase,label])=>`<span class="${phase===ex.phase?'current':''}">${esc(phase==='diagnosing'&&ex.feedback_mode==='none'?'基于公开契约提出假设':label)}</span>`).join('')}</div><div class="evo-budget"><span>开发集 ${development.filter(r=>['passed','failed'].includes(r.status)).length}/6 次有效 · ${development.filter(r=>r.status==='passed').length} 次通过</span><span>修改 ${esc(ex.target)}</span><span>诊断/生成 ${esc(ex.optimizer_model.model)}</span><span>优化调用 ${(ex.optimizer_calls||[]).length}/${(ex.optimizer_policy?.max_diagnosis_attempts||1)+1}</span></div>`:'';
  $('experiment-body').innerHTML=`${progress}<div class="evo-result-head"><strong>${esc(ex.status==='cancelled'?'已取消':decision?(decisions[decision.status]||decision.status):(labels[ex.status]||ex.status))}${!decision&&evaluation&&!active(ex.status)?' · '+esc(outcomes[evaluation.outcome]):''}</strong><span>${esc(decision?(reasons[decision.reason]||decision.detail||decision.reason):scope)}</span></div>${ex.error?`<p class="evo-error evo-scope">${esc(ex.error)}</p>`:''}<div class="evo-pair">${cards}</div>${ex.plan?`<div class="evo-coverage"><table><thead><tr><th>场景 / 每个版本通过次数</th><th>基线</th><th>候选</th><th>差异</th><th>证据</th></tr></thead><tbody>${rows}</tbody></table><p>${evaluation?`${evaluation.valid_trials}/${evaluation.planned_trials} 次有效结果 · 候选关键约束违反 ${evaluation.critical_violations} 次`:'等待配对结果。'} · 未执行项不算失败</p></div>`:''}${budget?`<div class="evo-budget"><span>总请求 ${budget.calls}/${budget.limits.calls}</span><span>已知 Token ${number(budget.tokens)}/${number(budget.limits.tokens)}</span><span>待结算 ${budget.pending} · 用量未知 ${budget.unknown_usage_calls}</span><span>总时限 ${budget.limits.seconds}s</span></div>`:''}`;
  const cancelling=ex.control?.state==='cancelling';
  const controls=active(ex.status)?`<button data-control="cancel" ${cancelling?'disabled':''}>${cancelling?'正在取消…':'取消实验'}</button><span>${cancelling?'正在结算已发出的请求、停止执行并清理本实验资源。':'取消后保留已完成证据，停止派发新调用。'}</span>`:ex.status==='interrupted'?`<button data-control="resume" ${ex.recovery?.eligible?'':'disabled'}>从检查点继续</button><span>${ex.recovery?.eligible?'跳过已确认的 Trial，沿用剩余预算与原截止时间。':esc((ex.recovery?.reasons||[]).join('；'))}</span>`:'';
  if(controls)$('experiment-body').insertAdjacentHTML('afterbegin',`<div class="evo-budget evo-controls">${controls}</div>`);
  if(ex.resources?.some(r=>r.state!=='cleaned'))$('experiment-body').insertAdjacentHTML('beforeend','<p class="evo-error evo-scope">执行资源尚未全部核对完成，不能继续派发。</p>');
  options($('run-select'),ex.runs.length?ex.runs.map(r=>[r.id,`${r.phase==='development'?'开发':'验证'} · ${r.role==='baseline'?'基线':'候选'} · ${r.family?families[r.family]+' 场景 '+r.task.split('-').at(-1)+' · 第 '+(r.repeat+1)+' 次 · ':''}${labels[r.status]||r.status}`]):[['','暂无执行']],$('run-select').value);
  renderDiagnosis();
  renderEvidence();
  renderHome();
}
function renderDiagnosis(){
  const ex=state.experiment;$('reasoning').hidden=!ex?.automatic_evolution;if(!ex?.automatic_evolution)return;
  const diagnosis=ex.diagnosis;
  const key=JSON.stringify([ex.id,diagnosis,ex.proposal,ex.optimizer_attempts,ex.decision]);if(state.diagnosisKey===key)return;state.diagnosisKey=key;
  const content=diagnosis?`${diagnosis.observations.map(o=>`<article><b>${esc(o.summary)}</b><div class="evo-refs">${o.evidence.map(id=>`<button data-evidence="${esc(id)}">${esc(id)}</button>`).join('')}</div></article>`).join('')}<article><span class="evo-muted">待验证的假设</span><p>${esc(diagnosis.hypothesis.summary)}</p><p>预期：${esc(diagnosis.hypothesis.expected_effect)}</p><p>验证：${esc(diagnosis.hypothesis.validation)}</p></article>${ex.proposal?`<article><b>候选理由与风险</b><p>${esc(ex.proposal.rationale)}</p></article>`:''}`:`<p class="evo-muted">${esc(ex.decision?.detail||'开发基线完成后，模型会引用具体执行事件提出假设。验证与封存详情不进入生成输入。')}</p>`;
  $('diagnosis-body').innerHTML=content+`<div class="evo-refs">${(ex.optimizer_attempts||[]).map((a,i)=>`<button data-output="${esc(a.id)}">${i+1}. ${a.phase==='diagnosis'?'诊断':'生成'}原始响应 · ${a.status==='invalid'?'未通过校验':a.status==='valid'?'有效':'已返回'}</button>`).join('')}</div><pre id="optimizer-raw" hidden></pre>`;
}
function renderEvidence(){
  const run=state.experiment?.runs.find(r=>r.id===$('run-select').value);if(!run)return;
  $('run-evidence').innerHTML=`<div>${(run.checks||[]).map(c=>`<p class="${c.passed?'evo-ok':'evo-error'}">${c.passed?'✓':'×'} ${esc(c.label||c.name||c.id||JSON.stringify(c))}</p>`).join('')||'<p class="evo-muted">尚未取得业务验收结果。</p>'}</div><details><summary>版本加载、模型输入与独立验收证据</summary><pre>${esc(JSON.stringify({mounted:run.version_load||null,model_input:run.input_evidence||null,verifier:{mode:run.verifier_mode||null,attested:run.verifier_attested??null,execution_frozen:run.execution_frozen??null,task_checksum:run.task_checksum||null}},null,2))}</pre></details><details><summary>工具轨迹 · ${(run.trace||[]).length} 条</summary>${(run.trace||[]).map(event=>`<div class="evo-trace"><b>${esc(event.label)}</b><p>${esc(event.text)}</p></div>`).join('')}</details>`;
}
async function refresh(){
  try{
    const overview=await api('/api/evo'),overviewKey=JSON.stringify(overview);
    state.overview=overview;
    if(!state.selected&&state.overview.experiments.length)state.selected=state.overview.experiments[0].id;
    if(state.overviewKey!==overviewKey){renderOverview();state.overviewKey=overviewKey;}
    if(state.selected){const experiment=await api('/api/evo/experiments/'+encodeURIComponent(state.selected)),experimentKey=JSON.stringify(experiment);
      state.experiment=experiment;
      if(state.loadedId!==state.selected&&!state.draft){$('baseline').value=state.experiment.baseline;$('protocol').value=state.experiment.mode||'loading';protocolChanged();if(!state.experiment.automatic_evolution)$('candidate').value=state.experiment.candidate;else{$('feedback-mode').value=state.experiment.feedback_mode||'development';$('target').value=state.experiment.target;$('optimizer-profile').value=state.experiment.optimizer_model.profile;}previewSelection();state.loadedId=state.selected;}
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
    addSkill({name:path.split('/')[1],description:parent.skills_meta?.[path]?.description||(line?line.slice(13):''),body:parts.slice(1).join('\n---\n').replace(/^\n/,'').replace(/\n$/,'')});
  }
  $('version-dialog').showModal();
}
$('new-version').onclick=()=>openVersion();$('close-dialog').onclick=()=>$('version-dialog').close();$('add-skill').onclick=()=>addSkill();$('runner').onchange=runnerChanged;
$('version-list').onclick=event=>{const button=event.target.closest('[data-version]');if(button){state.selectedVersion=button.dataset.version;renderLibrary();}};
$('baseline').onchange=populateCandidates;$('candidate').onchange=previewSelection;$('run-select').onchange=renderEvidence;
$('history').onchange=()=>{state.selected=$('history').value;state.draft=false;refresh();};
$('version-form').onsubmit=async event=>{
  event.preventDefault();$('save-version').disabled=true;$('form-error').hidden=true;
  try{
    const skills=[...$('skills').children].map(row=>({name:row.querySelector('.skill-name').value,description:row.querySelector('.skill-description').value,body:row.querySelector('.skill-body').value}));
    const result=await api('/api/evo/versions',{name:$('version-name').value,runner:$('runner').value,model_profile:$('model-profile').value,parent:$('parent-id').value||null,policy:$('policy').value,skills});
    $('version-dialog').close();notice('版本已保存。内容和模型身份已固化。');await refresh();
    state.selectedVersion=result.id;renderLibrary();location.hash='versions';notice('版本已保存。可查看文件、导出版本包，或用它发起实验。');
  }catch(error){$('form-error').textContent=error.message;$('form-error').hidden=false;}finally{$('save-version').disabled=false;}
};
let submitKey=null;
const resumeKeys=new Map();
$('experiment-body').onclick=async event=>{
  const button=event.target.closest('[data-control]');if(!button||state.busy)return;
  const id=state.selected,action=button.dataset.control;state.busy=true;button.disabled=true;
  if(action==='resume'&&!resumeKeys.has(id))resumeKeys.set(id,crypto.randomUUID());
  try{await api(`/api/evo/experiments/${encodeURIComponent(id)}/${action}`,{},resumeKeys.get(id));notice(action==='resume'?'已从检查点继续，预算不会重置。':'已请求取消；正在保留证据并清理执行资源。');await refresh();}
  catch(error){notice(error.message);button.disabled=false;}finally{state.busy=false;}
};
function protocolChanged(){
  const mode=$('protocol').value,config=state.overview?.protocols?.[mode]||{limits:{calls:24,tokens:360000,seconds:900}};
  for(const key of ['calls','tokens','seconds'])$('budget-'+key).value=config.limits[key];
  $('protocol-hint').textContent=mode==='improve'?'6 次开发基线 + 最多 2 次诊断（含 1 次结构修正）+ 1 次候选生成 + 24 次配对验证。只修改一个文件，接受后更新本地推荐版本；不会部署外部系统。':mode==='smoke'?'3 类业务问题，每类抽取 1 道开发题。用于确认执行链路，不产生晋升结论。每题最多 12 次模型请求。':mode==='validation'?'6 道验证题，每题重复 2 次，交替执行版本。完整覆盖、单题不退化、总通过数提升且关键约束零违反，才满足质量门槛。':mode==='report'?'封存数据用于最终报告。结果不参与候选生成和选优；重复运行封存集会削弱其独立性。':'旧版单题加载检查。使用修正后的无副作用帮助命令，不改写已有历史记录。';
  if(state.overview)populateCandidates();
}
$('protocol').onchange=protocolChanged;
$('feedback-mode').onchange=previewSelection;
$('run-experiment').onclick=async()=>{
  if(state.busy)return;state.busy=true;populateCandidates();
  const payload={baseline:$('baseline').value,candidate:$('candidate').value,mode:$('protocol').value,limits:Object.fromEntries(['calls','tokens','seconds'].map(k=>[k,Number($('budget-'+k).value)]))};
  if(payload.mode==='improve'){delete payload.candidate;payload.target=$('target').value;payload.optimizer_profile=$('optimizer-profile').value;payload.feedback_mode=$('feedback-mode').value;const project=(state.overview.projects||[]).find(p=>p.current_version===payload.baseline&&(p.feedback_mode||'development')===payload.feedback_mode);if(project)payload.project=project.id;}
  const signature=JSON.stringify(payload);if(!submitKey||submitKey.signature!==signature)submitKey={signature,key:crypto.randomUUID()};
  try{const result=await api('/api/evo/experiments',payload,submitKey.key);state.selected=result.id;state.draft=false;submitKey=null;$('launch-config').open=false;notice('实验已提交。关闭页面不会取消执行。');await refresh();}catch(error){notice(error.message);}finally{state.busy=false;populateCandidates();}
};
$('diagnosis-body').onclick=async event=>{const output=event.target.closest('[data-output]');if(output){const selected=state.selected;try{const data=await api('/api/evo/optimizer-outputs/'+encodeURIComponent(output.dataset.output));if(selected===state.selected){$('optimizer-raw').hidden=false;$('optimizer-raw').textContent=data.raw_output;}}catch(error){notice(error.message);}return;}const button=event.target.closest('[data-evidence]');if(!button)return;const ref=button.dataset.evidence,item=state.experiment.evidence_bundle.evidence.find(e=>e.id===ref),runId=item?.source?.run_id;if(!runId){notice(item?`${ref} · 公开任务契约：${item.value}`:'未找到该证据');return;}$('run-select').value=runId;renderEvidence();notice(item?`${ref} · ${typeof item.value==='string'?item.value:JSON.stringify(item.value)}`:'未找到该证据');$('evidence').scrollIntoView({behavior:'smooth',block:'start'});};
function statusText(ex){return ex.status==='cancelled'?'已取消':ex.decision?(decisions[ex.decision.status]||ex.decision.status):active(ex.status)?labels[ex.status]:ex.evaluation?(outcomes[ex.evaluation.outcome]||labels[ex.status]):labels[ex.status]||ex.status;}
function timeText(value){return value?new Date(value).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';}
function renderHome(){
  const data=state.overview;if(!data)return;
  const latest=data.experiments[0],ex=state.experiment?.id===latest?.id?state.experiment:latest;
  if(ex){const e=ex.evaluation,b=ex.budget,target=ex.target?.split('/').slice(-2).join('/')||'人工候选配对';
    $('overview-summary').innerHTML=`<div class="evo-summary"><div class="evo-inline"><span class="eyebrow">LATEST EXPERIMENT / ${esc(ex.id.slice(-6))}</span><span class="evo-pill">${esc(statusText(ex))}</span></div><h2>${esc(ex.candidate?(active(ex.status)?'候选正在接受独立验证。':ex.decision?.status==='accepted'?'验证通过，采用新版本。':ex.evaluation?.complete?'验证完成，保留实测结果。':'候选已生成，证据仍不足。'):(active(ex.status)?(ex.feedback_mode==='none'?'从公开契约提出对照候选。':'从执行反馈开始改进。'):'本次停止，保留原始基线。'))}</h2><p><b>${esc(version(ex.baseline)?.name||ex.baseline)}</b> → ${esc(version(ex.candidate)?.name||(active(ex.status)?'等待生成候选':'未生成候选'))}</p><p>改进对象：${esc(target)}</p><p>${esc(b?.unknown_usage_calls?`${b.unknown_usage_calls} 次模型请求的用量未确认；已停止后续派发，剩余题目不计为失败。`:ex.decision?(reasons[ex.decision.reason]||ex.decision.detail||ex.decision.reason):active(ex.status)?(phases[ex.phase]||'正在收集执行证据'):'这是一场加载或配对检查，不会自动更新推荐版本。')}</p><progress max="${e?.planned_trials||1}" value="${e?.valid_trials||0}" aria-label="有效评测覆盖"></progress><small class="evo-muted">${e?`${e.valid_trials}/${e.planned_trials} 次有效结果`:'等待评测结果'} · ${esc(protocolLabel(ex))}</small><dl><div><dt>已知 Token</dt><dd>${number(b?.tokens)}</dd></div><div><dt>模型请求</dt><dd>${number(b?.calls)}</dd></div><div><dt>用量未知</dt><dd>${number(b?.unknown_usage_calls)}</dd></div></dl><button data-experiment="${esc(ex.id)}">查看变更与判定 →</button></div>`;
  }
  $('recent-experiments').innerHTML=data.experiments.length?data.experiments.slice(0,8).map(x=>`<button class="evo-experiment-row" data-experiment="${esc(x.id)}"><span><b>${esc(version(x.candidate)?.name||version(x.baseline)?.name||x.id)}</b><small>${esc(protocolLabel(x))} · ${esc(x.id.slice(-6))}</small></span><span>${esc(statusText(x))}<small>${esc(x.decision?(reasons[x.decision.reason]||x.decision.reason):'记录独立验收结果')}</small></span><span>${x.evaluation?`${x.evaluation.valid_trials}/${x.evaluation.planned_trials} 有效`:'加载检查'}</span><span>${esc(timeText(x.created))}</span><span>↗</span></button>`).join(''):`<div class="evo-empty"><p>还没有实验。接入模型并创建基线后，运行第一场改进。</p><button data-create-baseline ${data.models.profiles.length?'':'disabled'}>创建基线</button></div>`;
}
function renderProjects(){
  const data=state.overview;
  $('projects').innerHTML=data.projects.map(p=>`<article class="evo-version"><h3>${esc(p.name)}</h3><p>当前推荐</p><button data-version="${esc(p.current_version)}">${esc(version(p.current_version)?.name||p.current_version)} ↗</button><p>冻结起点：${esc(version(p.initial_version)?.name||p.initial_version)}</p><div class="evo-inline"><code>修订 ${p.revision}</code><button data-rollback="${esc(p.id)}" ${p.rollback_targets.length?'':'disabled'}>回滚推荐</button></div>${!p.rollback_targets.length?'<p>仍在初始版本，没有可回滚的已采用祖先。</p>':''}</article>`).join('')||'<p class="evo-scope">首次改进实验会冻结初始基线。候选通过准入后，才更新本地推荐。</p>';
  const changes=[...(data.project_changes||[]).map(c=>({...c,label:'回滚推荐',from:c.from,to:c.to})),...data.experiments.filter(x=>x.decision?.status==='accepted').map(x=>({created:x.decision.created,label:'验证通过，采用候选',from:x.baseline,to:x.candidate,reason:reasons[x.decision.reason]}))].sort((a,b)=>b.created.localeCompare(a.created));
  $('project-history').innerHTML=changes.map(c=>`<article class="evo-project-change"><b>${esc(c.label)}</b> · ${esc(timeText(c.created))}<p>${esc(version(c.from)?.name||c.from)} → ${esc(version(c.to)?.name||c.to)}</p><p>${esc(c.reason)}</p></article>`).join('')||'<p class="evo-scope">尚无采用或回滚记录。未通过验证的候选不会自动成为推荐版本。</p>';
}
function renderLibrary(){
  const data=state.overview;if(!version(state.selectedVersion))state.selectedVersion=data.projects[0]?.current_version||data.versions[0]?.id;
  $('version-list').innerHTML=data.versions.map(v=>`<article class="evo-version ${v.id===state.selectedVersion?'selected':''}"><button class="version-open" data-version="${esc(v.id)}">${esc(v.name)}</button><p>${esc(data.capabilities[v.manifest.runner].name)} · ${esc(v.manifest.model.model)}</p><p>${Object.keys(v.manifest.files).length-1} 个 Skill · ${v.parent?'候选':'本地基线'}${data.projects.some(p=>p.current_version===v.id)?' · 当前推荐':''}</p><code>${esc(v.id.slice(3,15))}</code></article>`).join('')||'<div class="evo-empty">保存第一个策略版本，或导入已有版本包。</div>';
  const previousFile=$('asset-file')?.value;const v=version(state.selectedVersion);if(!v)return;
  const lineage=[];let p=v;while(p&&lineage.length<20){lineage.unshift(p);p=version(p.parent);}
  const experiments=data.experiments.filter(x=>x.baseline===v.id||x.candidate===v.id);
  const imported=(data.imports||[]).find(x=>x.version===v.id);
  $('version-detail').innerHTML=`<div class="evo-panel-head"><h2>${esc(v.name)}</h2><a class="evo-link-button" href="/api/evo/versions/${encodeURIComponent(v.id)}/bundle">导出版本包 ↓</a></div><div class="evo-asset-meta"><p>${esc(data.capabilities[v.manifest.runner].name)} ${esc(v.manifest.runtime_version)} · ${esc(v.manifest.model.model)}</p><p>内容指纹 ${esc(v.manifest_sha256)}</p><p>模型连接随版本冻结；导出包包含文件与模型 ID，导入时重新绑定本地连接。</p>${imported?'<p>由版本包导入：保留文件内容，评测结论需在本地重新建立。</p>':''}<div class="evo-inline"><button data-start-version="${esc(v.id)}" class="primary">改进这个版本</button><button data-parent="${esc(v.id)}">手动创建候选</button></div></div><div class="evo-lineage">${lineage.map(x=>`<button data-version="${esc(x.id)}">${esc(x.name)}</button>`).join('<span>→</span>')}</div><div class="evo-panel-head"><label>查看文件 <select id="asset-file" aria-label="版本文件"></select></label><span class="evo-muted">只读快照</span></div><pre id="asset-content" class="evo-diff evo-file-content"></pre><div class="evo-version-runs"><h3>此版本的独立验证记录</h3>${experiments.length?experiments.map(x=>`<button data-experiment="${esc(x.id)}">${esc(timeText(x.created))} · ${esc(statusText(x))} · ${x.evaluation?`${x.evaluation.valid_trials}/${x.evaluation.planned_trials} 有效`:'加载检查'}<br>任务包 ${esc(x.task_pack)} · ${esc(x.mode||'loading')}</button>`).join(''):'<p class="evo-muted">此版本尚无验证记录。父版本的结论不会自动继承。</p>'}</div>`;
  options($('asset-file'),Object.keys(v.manifest.files).map(p=>[p,p]),previousFile);$('asset-file').onchange=()=>$('asset-content').textContent=v.manifest.files[$('asset-file').value];$('asset-file').onchange();
}
function startExperiment(id){state.draft=true;location.hash='experiments';$('launch-config').open=true;if(id)$('baseline').value=id;$('protocol').value='improve';protocolChanged();$('launch-config').scrollIntoView({block:'center',behavior:'smooth'});}
function applyRoute(){const name=location.hash.slice(1).split('/')[0]||'overview',view=['overview','experiments','versions','tasks'].includes(name)?name:'overview';document.querySelectorAll('[data-view]').forEach(e=>e.hidden=e.dataset.view!==view);document.querySelectorAll('[data-nav]').forEach(e=>{if(e.dataset.nav===view)e.setAttribute('aria-current','page');else e.removeAttribute('aria-current');});if(view==='tasks')loadTasks();if(view==='experiments'&&state.draft)$('launch-config').scrollIntoView({block:'center'});else window.scrollTo(0,0);}
window.addEventListener('hashchange',applyRoute);
$('open-experiment').onclick=()=>{if(!state.overview.models.profiles.length){location.href='/settings';return;}if(!state.overview.versions.length){openVersion();return;}startExperiment(state.overview?.projects[0]?.current_version||$('baseline').value);};
$('new-experiment').onclick=()=>startExperiment($('baseline').value);
document.addEventListener('click',event=>{
  const trial=event.target.closest('[data-trial]');if(trial){$('run-select').value=trial.dataset.trial;renderEvidence();$('evidence').scrollIntoView({block:'start',behavior:'smooth'});return;}
  const ex=event.target.closest('[data-experiment]');if(ex){state.selected=ex.dataset.experiment;state.draft=false;location.hash='experiments';refresh();return;}
  const v=event.target.closest('[data-version]');if(v&&!$('version-list').contains(v)){state.selectedVersion=v.dataset.version;renderLibrary();location.hash='versions';return;}
  const start=event.target.closest('[data-start-version]');if(start){startExperiment(start.dataset.startVersion);return;}
  const parent=event.target.closest('[data-parent]');if(parent){openVersion(parent.dataset.parent);return;}
  if(event.target.closest('[data-create-baseline]'))openVersion();
  const close=event.target.closest('[data-close]');if(close)$(close.dataset.close).close();
  const rb=event.target.closest('[data-rollback]');if(rb)openRollback(rb.dataset.rollback);
});
let rollbackContext=null;
function openRollback(id){const p=state.overview.projects.find(p=>p.id===id);rollbackContext={project:p,key:crypto.randomUUID()};$('rollback-form').reset();$('rollback-current').textContent=`当前：${version(p.current_version)?.name||p.current_version} · 修订 ${p.revision}`;options($('rollback-target'),p.rollback_targets.map(id=>[id,version(id)?.name||id]));$('rollback-status').textContent='';$('rollback-dialog').showModal();}
$('rollback-form').onsubmit=async event=>{event.preventDefault();$('rollback-submit').disabled=true;const {project:p,key}=rollbackContext;try{await api(`/api/evo/projects/${encodeURIComponent(p.id)}/rollback`,{target:$('rollback-target').value,expected_version:p.current_version,expected_revision:p.revision,reason:$('rollback-reason').value},key);$('rollback-dialog').close();notice('推荐版本已回滚，原实验与判定保留。');await refresh();}catch(error){$('rollback-status').textContent=error.message;}finally{$('rollback-submit').disabled=false;}};
$('import-version').onclick=()=>{$('import-form').reset();options($('import-profile'),state.overview.models.profiles.map(p=>[p.id,`${p.name} · ${p.model}`]),state.overview.models.default);$('import-status').textContent='';$('import-dialog').showModal();};
$('import-form').onsubmit=async event=>{event.preventDefault();$('import-submit').disabled=true;try{const file=$('bundle-file').files[0];if(!file||file.size>100000)throw new Error('请选择不超过 100 KB 的版本包');const bytes=new Uint8Array(await file.arrayBuffer());let binary='';for(const byte of bytes)binary+=String.fromCharCode(byte);const result=await api('/api/evo/import',{archive:btoa(binary),model_profile:$('import-profile').value});$('import-dialog').close();state.selectedVersion=result.id;notice('版本已导入，文件内容校验通过。验证结论需在本地重新建立。');await refresh();renderLibrary();}catch(error){$('import-status').textContent=error.message;}finally{$('import-submit').disabled=false;}};
let tasks=null;
$('show-example').onclick=async()=>{try{const data=await api('/api/evo/example'),ex=data.experiment,dev=ex.runs.filter(r=>r.phase==='development');$('example-body').innerHTML=`<p class="evo-pill">真实执行 · ${esc(ex.model)} · Pi · 已脱敏</p><h3>生成了新 Skill，但证据不足以采用。</h3><p>2026-11-10 的实验记录。这份回放不加入你的实验、预算或推荐版本。</p><ol><li>开发基线：${dev.filter(r=>r.status==='passed').length}/${dev.length} 通过。</li><li>模型根据开发记录提出假设，生成 <code>${esc(ex.target)}</code> 的新版本。</li><li>候选由真实 Pi 加载，与基线配对；取得 ${ex.evaluation.valid_trials}/${ex.evaluation.planned_trials} 次有效验证。</li><li>一次模型响应异常且用量未知，停止派发。最终证据不足，原推荐版本保留。</li></ol><p>总请求 ${ex.budget.calls} · 已知 Token ${number(ex.budget.tokens)} · 用量未知 ${ex.budget.unknown_usage_calls} 次。</p><details><summary>按执行顺序查看结果</summary><ol>${ex.runs.map(r=>`<li>${esc(r.phase==='development'?'开发':'验证')} · ${esc(r.role==='baseline'?'基线':'候选')} · ${esc(r.task)} · ${esc(labels[r.status]||r.status)}</li>`).join('')}</ol></details><p>来源：${esc(data.source_document)} · ${esc(data.source_commit.slice(0,12))}</p><p>这是一次部分验证后的停止记录，不能推导为候选有收益或完整协议通过。</p>`;$('example-dialog').showModal();}catch(error){notice(error.message);}};
async function loadTasks(){try{if(!tasks)tasks=await api('/api/evo/taskpacks');options($('taskpack-select'),tasks.packs.map(p=>[p.id,`${p.id===tasks.active?'当前可执行':'历史记录'} · ${p.id.slice(-12)}`]),$('taskpack-select').value||tasks.active);renderTasks();}catch(error){notice(error.message);}}
function renderTasks(){const pack=tasks.packs.find(p=>p.id===$('taskpack-select').value),current=pack.id===tasks.active;$('taskpack-state').textContent=current?'新实验使用此冻结任务包':'历史包仅查看；新实验使用当前执行代码';$('taskpack-id').textContent=pack.id;$('calibrate').disabled=!current;const descriptions={receipt:['结果不确定时，确认订单恰好创建一次。','验收：最终订单数量、可信状态观察与重复写入。'],listing:['从分页与延迟索引中，核对完整、一致的订单清单。','验收：客户、订单状态、最新修订、ID 集合与金额。'],workflow:['库存与确认部分成功时，正确完成或补偿整条业务流程。','验收：占用数量、确认状态、取消依据与库存释放。']};$('task-families').innerHTML=Object.entries(families).map(([key,label],i)=>`<article class="evo-task-card"><span>0${i+1} / ${esc(key.toUpperCase())}</span><h2>${esc(label)}</h2><p>${descriptions[key][0]}</p><b>${descriptions[key][1]}</b><p>开发 2 · 验证 2 · 封存 2</p></article>`).join('');$('task-cases').innerHTML=[['development','开发集','用于观察执行反馈与提出候选'],['validation','验证集','用于同题配对与准入判定'],['sealed','封存集','只用于最终报告，不回流候选生成']].map(([key,label,hint])=>`<details ${key==='development'?'open':''}><summary>${label} · ${pack.cases.filter(c=>c.split===key).length} 题 <span class="evo-muted"> / ${hint}</span></summary><ul>${pack.cases.filter(c=>c.split===key).map(c=>`<li><span>${esc(c.title)} · ${esc(c.id)}</span><code>${esc(c.checksum.slice(0,16))}</code></li>`).join('')}</ul></details>`).join('');}
$('taskpack-select').onchange=renderTasks;
$('calibrate').onclick=async()=>{$('calibrate').disabled=true;$('calibration-result').textContent='正在检查正确解、空操作与错误解…';try{const r=await api('/api/evo/calibrate',{});$('calibration-result').textContent=`${r.passed?'✓ 校准通过':'× 校准未通过'}：正确解 ${r.correct_accepted}/${r.cases} 接受；空操作 ${r.empty_rejected}/${r.cases} 拒绝；错误解 ${r.bad_rejected}/${r.cases} 拒绝。模型调用 ${r.model_calls} 次；这不是 Agent 成绩。`;}catch(error){$('calibration-result').textContent=error.message;}finally{$('calibrate').disabled=false;}};
applyRoute();refresh();setInterval(()=>{if(!document.querySelector('dialog[open]')&&!state.busy)refresh();},4000);
