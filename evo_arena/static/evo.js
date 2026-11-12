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
function notice(message,source='action') {$('notice').hidden=!message;$('notice').textContent=message;$('notice').dataset.source=source;}
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
  $('run-experiment').disabled=state.busy||!state.overview.ready||(auto?!parent:!$('candidate').value)||state.overview.experiments.some(row=>active(row.status))||(state.overview.campaigns||[]).some(c=>campaignActive(c.status));
  stageChanged(false);previewSelection();
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
  const data=state.overview;renderWorkspace();
  $('version-count').textContent=selectedProject()?.version_ids.length||data.versions.length;$('experiment-count').textContent=projectExperiments().length;
  options($('baseline'),data.versions.length?data.versions.map(row=>[row.id,row.name]):[['','请先创建基线']],$('baseline').value);
  if(selectedProject())$('baseline').value=selectedProject().current_version;
  populateCandidates();
  options($('history'),projectExperiments().length?projectExperiments().map(row=>[row.id,`${row.id.slice(-6)} · ${statusText(row)}`]):[['','暂无实验']],state.selected);
  renderLibrary();renderHome();renderProjects();renderCampaigns();
  $('capabilities').innerHTML=Object.entries(data.capabilities).map(([id,c])=>`<div class="evo-capability"><strong>${esc(c.name)} <span class="evo-muted">${esc(c.runtime_version)}</span></strong><p>${id==='pi'?'系统提示追加 · 原生 Skill 文件':'任务指令前缀 · 原生 Skill 未验证'}</p></div>`).join('');
  $('new-version').disabled=!data.models.profiles.length;
  $('open-experiment').textContent=!data.models.profiles.length?'连接模型 ↗':!data.versions.length?'创建基线 ＋':'发起改进 ↗';
  if(!state.protocolInitialized){protocolChanged();state.protocolInitialized=true;}
  if(!data.ready)notice('运行实验需要可用的模型连接和 Harbor CLI。已有版本与记录仍可查看。');
}
function renderExperiment() {
  const ex=state.experiment;if(!ex)return;
  const same=state.disclosureExperiment===ex.id,expanded=new Set(same?[...document.querySelectorAll('details[id][open]')].map(d=>d.id):[]);
  if(!same){for(const id of ['reasoning','changes','evidence'])$(id).open=false;state.disclosureExperiment=ex.id;}
  $('export-report').hidden=false;$('export-report').href=`/api/evo/experiments/${encodeURIComponent(ex.id)}/report`;
  $('experiment-title').textContent=`${shortMode(ex)} · ${ex.id.slice(-6)}`;
  $('history').value=ex.id;
  $('version-diff').textContent=ex.diff||'暂无文件差异。';
  const lines=(ex.diff||'').split('\n');$('diff-count').textContent=ex.diff?`+${lines.filter(l=>l.startsWith('+')&&!l.startsWith('+++')).length} −${lines.filter(l=>l.startsWith('-')&&!l.startsWith('---')).length}`:'';
  const evaluation=ex.evaluation,budget=ex.budget,stageOnly=['diagnose','propose'].includes(ex.stage);
  const cards=['baseline','candidate'].map(role=>{
    const v=version(ex[role]),runs=ex.runs.filter(r=>r.role===role&&(!ex.automatic_evolution||r.phase==='validation')),run=runs.at(-1);
    const planned=ex.plan?.trials.filter(r=>r.role===role).length,done=runs.filter(r=>['passed','failed'].includes(r.status)&&!evaluation?.missing?.includes(r.key));
    const score=stageOnly?null:ex.plan?`${done.filter(r=>r.status==='passed').length}<small> / ${planned}</small>`:run?.checks?.length?`${run.checks.filter(c=>c.passed).length}<small> / ${run.checks.length}</small>`:'—';
    return `<article class="evo-side"><span class="evo-role">${role==='baseline'?'基线':'候选'}</span><h3>${esc(v?.name||'尚未生成')}</h3>${score!==null?`<div class="evo-score">${score}</div><p>${ex.plan?'已确认通过':'验收通过'}</p>`:`<p>${role==='candidate'&&v?'待验证':'当前版本'}</p>`}</article>`;
  }).join('');
  const tasks=ex.plan?[...new Set(ex.plan.trials.map(r=>r.task))]:[];
  const rows=tasks.map(task=>{const spec=ex.plan.trials.find(r=>r.task===task),g=evaluation?.per_case?.[task],complete=g&&g.baseline_completed===ex.plan.repeats&&g.candidate_completed===ex.plan.repeats,delta=complete?g.candidate-g.baseline:null,run=ex.runs.findLast(r=>r.task===task&&r.role==='candidate')||ex.runs.findLast(r=>r.task===task);return `<tr><td>${esc(families[spec.family])}<br><small>${esc(task)}</small></td><td>${g?`${g.baseline}/${ex.plan.repeats}`:'—'}</td><td>${g?`${g.candidate}/${ex.plan.repeats}`:'—'}</td><td class="${delta>0?'evo-ok':delta<0?'evo-error':''}">${delta===null?'未齐':delta>0?'＋'+delta:delta<0?String(delta):'持平'}</td><td>${run?`<button data-trial="${esc(run.id)}">查看</button>`:'—'}</td></tr>`;}).join('');
  const coverage=evaluation?`${evaluation.valid_trials}/${evaluation.planned_trials} 有效验证`:stageOnly?'尚未验证':active(ex.status)?'验证进行中':'尚无完整验证';
  const unknown=budget?.unknown_usage_calls||0,pending=budget?.pending||0;
  const coverageDetail=ex.plan&&!stageOnly?`<details class="evo-disclosure" id="case-results"><summary>逐题结果 <span>${tasks.length} 个场景</span></summary><div class="evo-coverage"><table><thead><tr><th>场景</th><th>基线</th><th>候选</th><th>变化</th><th>证据</th></tr></thead><tbody>${rows}</tbody></table><p>未运行项不计为失败${evaluation?` · 关键约束违反 ${evaluation.critical_violations} 次`:''}</p></div></details>`:'';
  const runtime=`<details class="evo-disclosure" id="runtime-details"><summary>运行详情 <span>${number(budget?.calls)} 次请求 · ${compactNumber(budget?.tokens)} 已知 Token</span></summary><div class="evo-help-body"><p>${esc(ex.runner)} · ${esc(ex.model)}</p>${ex.source?'<p>开发证据复用来源实验，本次不重复计费。</p>':''}${ex.automatic_evolution?`<p>诊断模型：${esc(ex.optimizer_model.model)} · 本阶段优化调用 ${(ex.optimizer_calls||[]).length} 次</p>`:''}${ex.error?`<p class="evo-error">${esc(ex.error)}</p>`:''}${ex.decision?`<p>${esc(reasons[ex.decision.reason]||ex.decision.detail||ex.decision.reason)}</p>`:''}${budget?`<p>请求 ${budget.calls}/${budget.limits.calls} · 已知 Token ${number(budget.tokens)}/${number(budget.limits.tokens)} · ${budget.limits.seconds}s</p><p>待结算 ${pending} · 用量未知 ${unknown}</p>`:''}</div></details>`;
  $('experiment-body').innerHTML=`<div class="evo-result-head"><div><strong>${esc(statusText(ex))}</strong><p>${esc(reasonBrief(ex))}</p></div><span class="evo-pill">${coverage}</span></div>${unknown||pending?`<div class="evo-cost-alert">${unknown?`${unknown} 次用量未知`:''}${unknown&&pending?' · ':''}${pending?`${pending} 次待结算`:''}</div>`:''}<div class="evo-pair">${cards}</div>${coverageDetail}${runtime}`;
  const cancelling=ex.control?.state==='cancelling';
  const controls=active(ex.status)?`<button data-control="cancel" ${cancelling?'disabled':''}>${cancelling?'正在取消…':'取消实验'}</button>`:ex.status==='interrupted'?`<button data-control="resume" ${ex.recovery?.eligible?'':'disabled'}>从检查点继续</button><span>${ex.recovery?.eligible?'沿用剩余预算':esc((ex.recovery?.reasons||[]).join('；'))}</span>`:'';
  if(controls)$('experiment-body').insertAdjacentHTML('afterbegin',`<div class="evo-budget evo-controls">${controls}</div>`);
  if(!active(ex.status)&&ex.resources?.some(r=>r.state!=='cleaned'))$('experiment-body').insertAdjacentHTML('beforeend','<p class="evo-error evo-scope">资源尚未清理完成</p>');
  options($('run-select'),ex.runs.length?ex.runs.map(r=>[r.id,`${r.phase==='development'?'开发':'验证'} · ${r.role==='baseline'?'基线':'候选'} · ${r.family?families[r.family]+' '+r.task.split('-').at(-1)+' · 第 '+(r.repeat+1)+' 次 · ':''}${labels[r.status]||r.status}`]):[['','暂无执行']],$('run-select').value);
  for(const id of expanded){const element=$(id);if(element?.tagName==='DETAILS')element.open=true;}
  renderJourney();renderDiagnosis();renderEvidence();renderHome();
}
function renderDiagnosis(){
  const ex=state.experiment;$('reasoning').hidden=!ex?.automatic_evolution;if(!ex?.automatic_evolution)return;
  const diagnosis=ex.diagnosis;
  const key=JSON.stringify([ex.id,diagnosis,ex.proposal,ex.optimizer_attempts,ex.decision]);if(state.diagnosisKey===key)return;state.diagnosisKey=key;
  const content=diagnosis?`${diagnosis.observations.map(o=>`<article><b>${esc(o.summary)}</b><div class="evo-refs">${o.evidence.map(id=>`<button data-evidence="${esc(id)}">${esc(id)}</button>`).join('')}</div></article>`).join('')}<article><span class="evo-muted">待验证的假设</span><p>${esc(diagnosis.hypothesis.summary)}</p><p>预期：${esc(diagnosis.hypothesis.expected_effect)}</p><p>验证：${esc(diagnosis.hypothesis.validation)}</p></article>${ex.proposal?`<article><b>候选理由与风险</b><p>${esc(ex.proposal.rationale)}</p></article>`:''}`:`<p class="evo-muted">${esc(ex.decision?.detail||'开发基线完成后，模型会引用具体执行事件提出假设。验证与封存详情不进入生成输入。')}</p>`;
  $('diagnosis-body').innerHTML=content+`<div class="evo-refs">${(ex.optimizer_attempts||[]).map((a,i)=>`<button data-output="${esc(a.id)}">${i+1}. ${a.phase==='diagnosis'?'诊断':'生成'}原始响应 · ${a.status==='invalid'?'未通过校验':a.status==='valid'?'有效':'已返回'}</button>`).join('')}</div><pre id="optimizer-raw" hidden></pre>`;
}
function renderEvidence(){
  const run=state.experiment?.runs.find(r=>r.id===$('run-select').value);if(!run){$('run-evidence').textContent='此阶段未运行新任务，开发证据见来源实验。';return;}
  const opened=state.evidenceRun===run.id?[...$('run-evidence').querySelectorAll('details')].map(d=>d.open):[];state.evidenceRun=run.id;
  $('run-evidence').innerHTML=`<div>${(run.checks||[]).map(c=>`<p class="${c.passed?'evo-ok':'evo-error'}">${c.passed?'✓':'×'} ${esc(c.label||c.name||c.id||JSON.stringify(c))}</p>`).join('')||'<p class="evo-muted">尚未取得业务验收结果。</p>'}</div><details><summary>版本加载、模型输入与独立验收证据</summary><pre>${esc(JSON.stringify({mounted:run.version_load||null,model_input:run.input_evidence||null,verifier:{mode:run.verifier_mode||null,attested:run.verifier_attested??null,execution_frozen:run.execution_frozen??null,task_checksum:run.task_checksum||null}},null,2))}</pre></details><details><summary>工具轨迹 · ${(run.trace||[]).length} 条</summary>${(run.trace||[]).map(event=>`<div class="evo-trace"><b>${esc(event.label)}</b><p>${esc(event.text)}</p></div>`).join('')}</details>`;
  $('run-evidence').querySelectorAll('details').forEach((d,i)=>d.open=Boolean(opened[i]));
}
async function refresh(){
  try{
    const overview=await api('/api/evo'),overviewKey=JSON.stringify(overview);
    state.overview=overview;
    if(state.project===undefined)state.project=localStorage.getItem("evo.project")||"";
    if(!state.selected&&projectExperiments().length)state.selected=projectExperiments()[0].id;
    if(state.overviewKey!==overviewKey){renderOverview();state.overviewKey=overviewKey;}
    if(state.selected){const experiment=await api('/api/evo/experiments/'+encodeURIComponent(state.selected)),experimentKey=JSON.stringify(experiment);
      state.experiment=experiment;
      // A fast stage can finish between the overview and detail responses.
      // Next-stage sources must use the newest confirmed detail, not that older list.
      state.overview.experiments=state.overview.experiments.map(row=>row.id===experiment.id?{...row,...experiment}:row);
      if(state.loadedId!==state.selected&&!state.draft){$('baseline').value=state.experiment.baseline;$('protocol').value=state.experiment.mode||'loading';protocolChanged();if(!state.experiment.automatic_evolution)$('candidate').value=state.experiment.candidate;else{$('feedback-mode').value=state.experiment.feedback_mode||'development';$('target').value=state.experiment.target;$('optimizer-profile').value=state.experiment.optimizer_model.profile;}previewSelection();state.loadedId=state.selected;}
      if(state.experimentKey!==experimentKey){renderExperiment();state.experimentKey=experimentKey;}}
    if(!state.selected){state.experiment=null;$('experiment-title').textContent='实验详情';$('experiment-body').innerHTML='<div class="evo-empty">本项目还没有实验，可从分析执行反馈开始。</div>';$('comparison-summary').innerHTML='';$('reasoning').hidden=true;$('diff-count').textContent='';for(const id of ['changes','evidence'])$(id).open=false;$('version-diff').textContent='候选生成后显示实际变更。';$('run-evidence').textContent='尚无执行记录。';$('export-report').hidden=true;}
    if($('notice').dataset.source==='refresh')notice('');
  }catch(error){notice(error.message,'refresh');}
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
$('feedback-mode').onchange=()=>{stageChanged(false);previewSelection();};
$('run-experiment').onclick=async()=>{
  if(state.busy)return;state.busy=true;populateCandidates();
  const payload={baseline:$('baseline').value,candidate:$('candidate').value,mode:$('protocol').value,limits:Object.fromEntries(['calls','tokens','seconds'].map(k=>[k,Number($('budget-'+k).value)]))};
  if(payload.mode==='improve'){delete payload.candidate;payload.target=$('target').value;payload.optimizer_profile=$('optimizer-profile').value;payload.feedback_mode=$('feedback-mode').value;payload.stage=$('improve-stage').value;const project=selectedProject()||(state.overview.projects||[]).find(p=>p.current_version===payload.baseline&&(p.feedback_mode||'development')===payload.feedback_mode);if(project)payload.project=project.id;if(payload.stage!=='full'&&$('stage-source').value)payload.source=$('stage-source').value;}
  const signature=JSON.stringify(payload);if(!submitKey||submitKey.signature!==signature)submitKey={signature,key:crypto.randomUUID()};
  try{const result=await api('/api/evo/experiments',payload,submitKey.key);state.selected=result.id;state.draft=false;submitKey=null;$('launch-config').open=false;notice('实验已提交。关闭页面不会取消执行。');await refresh();}catch(error){notice(error.message);}finally{state.busy=false;populateCandidates();}
};
$('diagnosis-body').onclick=async event=>{const output=event.target.closest('[data-output]');if(output){const selected=state.selected;try{const data=await api('/api/evo/optimizer-outputs/'+encodeURIComponent(output.dataset.output));if(selected===state.selected){$('optimizer-raw').hidden=false;$('optimizer-raw').textContent=data.raw_output;}}catch(error){notice(error.message);}return;}const button=event.target.closest('[data-evidence]');if(!button)return;const ref=button.dataset.evidence,item=state.experiment.evidence_bundle.evidence.find(e=>e.id===ref),runId=item?.source?.run_id;if(!runId){notice(item?`${ref} · 公开任务契约：${item.value}`:'未找到该证据');return;}if(!state.experiment.runs.some(r=>r.id===runId)&&state.experiment.source){state.selected=state.experiment.source_snapshot?.source_chain?.at(-1)||state.experiment.source;state.draft=false;await refresh();}$('run-select').value=runId;renderEvidence();notice(item?`${ref} · ${typeof item.value==='string'?item.value:JSON.stringify(item.value)}`:'未找到该证据');reveal('evidence');};
function statusText(ex){if(ex.status==='completed'&&ex.phase==='diagnosed')return '诊断已保存';if(ex.status==='completed'&&ex.phase==='candidate_ready')return '候选待验证';return ex.status==='cancelled'?'已取消':ex.decision?(decisions[ex.decision.status]||ex.decision.status):active(ex.status)?labels[ex.status]:ex.evaluation?(outcomes[ex.evaluation.outcome]||labels[ex.status]):labels[ex.status]||ex.status;}
function timeText(value){return value?new Date(value).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';}
function compactNumber(value){return typeof value==='number'?new Intl.NumberFormat('en',{notation:'compact',maximumFractionDigits:2}).format(value):'—';}
function shortMode(ex){return ex.stage&&ex.stage!=='full'?stageLabels[ex.stage]:{improve:'自动改进',smoke:'冒烟检查',validation:'配对验证',report:'封存评测',loading:'加载检查'}[ex.mode]||'加载检查';}
function outcomeTitle(ex){
  if(active(ex.status))return phases[ex.phase]||'正在执行';
  if(ex.status==='cancelled')return '已取消';
  if(ex.phase==='diagnosed')return '诊断已就绪';
  if(ex.phase==='candidate_ready')return '候选待验证';
  if(ex.decision?.status==='accepted')return '已采用新版本';
  if(ex.decision?.status==='not_applied')return '推荐已变化，未采用';
  if(['incomplete','interrupted'].includes(ex.status)||ex.evaluation?.outcome==='insufficient_evidence')return ex.plan||ex.mode==='improve'?'验证未完成':'执行未完成';
  if(ex.evaluation?.outcome==='unchanged')return '未观察到提升';
  if(ex.evaluation?.outcome==='regression')return '候选发生退化';
  return statusText(ex);
}
function reasonBrief(ex){
  if(ex.budget?.unknown_usage_calls)return '用量未知，后续执行已停止';
  if(ex.phase==='diagnosed')return '下一步：生成候选';
  if(ex.phase==='candidate_ready')return '下一步：独立验证';
  if(ex.decision?.status==='accepted')return '通过验证，推荐已更新';
  return ({execution_error:'执行异常，推荐未变',provider_error:'模型请求异常',budget_exhausted:'已达到预算限制',incomplete_coverage:'验证尚未完整覆盖',regression:'存在退化，保留原版本',unchanged:'没有新增通过项',invalid_candidate:'候选未通过校验',invalid_diagnosis:'诊断未通过校验',project_changed:'项目已更新'})[ex.decision?.reason]||'';
}
function renderHome(){
  const data=state.overview;if(!data)return;
  const history=projectExperiments(),latest=history.find(e=>e.mode==='improve')||history[0],ex=state.experiment?.id===latest?.id?state.experiment:latest;
  if(ex){const e=ex.evaluation,b=ex.budget;
    $('overview-summary').innerHTML=`<div class="evo-summary"><div class="evo-inline"><span class="eyebrow">最近改进</span><small class="evo-muted">${esc(timeText(ex.created))}</small></div><h2>${esc(outcomeTitle(ex))}</h2><p>${esc(reasonBrief(ex))}</p><dl><div><dt>有效验证</dt><dd>${e?`${e.valid_trials}<small> / ${e.planned_trials}</small>`:'—'}</dd></div><div><dt>已知 Token</dt><dd>${compactNumber(b?.tokens)}</dd></div><div><dt>${b?.unknown_usage_calls?'用量未知':'模型请求'}</dt><dd class="${b?.unknown_usage_calls?'evo-error':''}">${number(b?.unknown_usage_calls||b?.calls)}${b?.unknown_usage_calls?'<small> 次</small>':''}</dd></div></dl><button data-experiment="${esc(ex.id)}">查看实验 →</button></div>`;
  }else $('overview-summary').innerHTML=`<div class="evo-empty"><h2>开始第一次改进</h2><p>${data.versions.length?'选择基线，分析执行反馈。':'先配置模型并保存一个基线版本。'}</p></div>`;
  const row=x=>`<button class="evo-experiment-row" data-experiment="${esc(x.id)}"><span><b>${esc(version(x.candidate)?.name||version(x.baseline)?.name||x.id)}</b><small>${esc(shortMode(x))}</small></span><span class="${x.decision?.status==='accepted'?'evo-ok':''}">${esc(statusText(x))}</span><span>${x.evaluation?`${x.evaluation.valid_trials}/${x.evaluation.planned_trials}`:'—'}</span><span>${esc(timeText(x.created))}</span><span>↗</span></button>`;
  const older=history.slice(4),olderOpen=$('older-experiments')?.open;
  $('recent-experiments').innerHTML=history.length?history.slice(0,4).map(row).join('')+(older.length?`<details class="evo-more-history" id="older-experiments" ${olderOpen?'open':''}><summary>更早的 ${older.length} 次实验</summary>${older.map(row).join('')}</details>`:''):'<div class="evo-empty">暂无实验</div>';
}
function renderProjects(){
  const data=state.overview,opened=new Set([...$('projects').querySelectorAll('details[open]')].map(d=>d.id));
  $('projects').innerHTML=data.projects.filter(p=>!state.project||p.id===state.project).map(p=>{const v=version(p.current_version);return `<article class="evo-recommendation"><span class="evo-pill">${p.current_version===p.initial_version?'沿用初始版本':`修订 ${p.revision}`}</span><h3>${esc(v?.name||p.current_version)}</h3><p>${esc(v?.manifest.runner)} · ${esc(v?.manifest.model.model)}</p><button data-version="${esc(p.current_version)}">查看版本 →</button><details class="evo-inline-details" id="history-${esc(p.id)}" ${opened.has('history-'+p.id)?'open':''}><summary>版本历史</summary><p>起点：${esc(version(p.initial_version)?.name||p.initial_version)}</p>${p.rollback_targets.length?`<button data-rollback="${esc(p.id)}">回滚推荐</button>`:'<p>暂无采用或回滚记录</p>'}</details></article>`;}).join('')||'<div class="evo-empty">创建项目后显示推荐版本</div>';
  const changes=[...(data.project_changes||[]).filter(c=>!state.project||c.project===state.project).map(c=>({...c,label:'回滚推荐',from:c.from,to:c.to})),...projectExperiments().filter(x=>x.decision?.status==='accepted').map(x=>({created:x.decision.created,label:'验证通过，采用候选',from:x.baseline,to:x.candidate,reason:reasons[x.decision.reason]}))].sort((a,b)=>b.created.localeCompare(a.created));
  $('project-history').innerHTML=changes.map(c=>`<article class="evo-project-change"><b>${esc(c.label)}</b> · ${esc(timeText(c.created))}<p>${esc(version(c.from)?.name||c.from)} → ${esc(version(c.to)?.name||c.to)}</p><p>${esc(c.reason)}</p></article>`).join('')||'<p class="evo-scope">尚无采用或回滚记录。未通过验证的候选不会自动成为推荐版本。</p>';
}
function renderLibrary(){
  const data=state.overview;if(!version(state.selectedVersion))state.selectedVersion=data.projects[0]?.current_version||data.versions[0]?.id;
  $('version-list').innerHTML=data.versions.map(v=>`<article class="evo-version ${v.id===state.selectedVersion?'selected':''}"><button class="version-open" data-version="${esc(v.id)}">${esc(v.name)}</button><p>${esc(data.capabilities[v.manifest.runner].name)} · ${esc(v.manifest.model.model)}</p><p>${Object.keys(v.manifest.files).length-1} 个 Skill · ${v.parent?'候选':'本地基线'}${data.projects.some(p=>p.current_version===v.id)?' · 当前推荐':''}</p><code>${esc(v.id.slice(3,15))}</code></article>`).join('')||'<div class="evo-empty">保存第一个策略版本，或导入已有版本包。</div>';
  const previousFile=$('asset-file')?.value;const v=version(state.selectedVersion);if(!v)return;
  const lineage=[];let p=v;while(p&&lineage.length<20){lineage.unshift(p);p=version(p.parent);}
  const experiments=data.experiments.filter(x=>x.baseline===v.id||x.candidate===v.id);
  const imported=(data.imports||[]).find(x=>x.version===v.id);
  $('version-detail').innerHTML=`<div class="evo-panel-head"><h2>${esc(v.name)}</h2><a class="evo-link-button" href="/api/evo/versions/${encodeURIComponent(v.id)}/bundle">导出版本包 ↓</a></div><div class="evo-asset-meta"><p>${esc(data.capabilities[v.manifest.runner].name)} ${esc(v.manifest.runtime_version)} · ${esc(v.manifest.model.model)}</p><details class="evo-inline-details"><summary>版本信息</summary><p>内容指纹 ${esc(v.manifest_sha256)}</p><p>导入时重新绑定本地模型连接。</p></details>${imported?'<p>由版本包导入：保留文件内容，评测结论需在本地重新建立。</p>':''}<div class="evo-inline"><button data-start-version="${esc(v.id)}" class="primary">改进这个版本</button><button data-parent="${esc(v.id)}">手动创建候选</button></div></div><div class="evo-lineage">${lineage.map(x=>`<button data-version="${esc(x.id)}">${esc(x.name)}</button>`).join('<span>→</span>')}</div><div class="evo-panel-head"><label>查看文件 <select id="asset-file" aria-label="版本文件"></select></label><span class="evo-muted">只读快照</span></div><pre id="asset-content" class="evo-diff evo-file-content"></pre><div class="evo-version-runs"><h3>此版本的独立验证记录</h3>${experiments.length?experiments.map(x=>`<button data-experiment="${esc(x.id)}">${esc(timeText(x.created))} · ${esc(statusText(x))} · ${x.evaluation?`${x.evaluation.valid_trials}/${x.evaluation.planned_trials} 有效`:'加载检查'}<br>任务包 ${esc(x.task_pack)} · ${esc(x.mode||'loading')}</button>`).join(''):'<p class="evo-muted">此版本尚无验证记录。父版本的结论不会自动继承。</p>'}</div>`;
  options($('asset-file'),Object.keys(v.manifest.files).map(p=>[p,p]),previousFile);$('asset-file').onchange=()=>$('asset-content').textContent=v.manifest.files[$('asset-file').value];$('asset-file').onchange();
}
function startExperiment(id){$('improve-stage').value='full';state.draft=true;location.hash='experiments';$('launch-config').open=true;if(id)$('baseline').value=id;$('protocol').value='improve';protocolChanged();$('launch-config').scrollIntoView({block:'center',behavior:'smooth'});}
function applyRoute(){const name=location.hash.slice(1).split('/')[0]||'overview',view=['overview','experiments','versions','tasks'].includes(name)?name:'overview';document.querySelectorAll('[data-view]').forEach(e=>e.hidden=e.dataset.view!==view);document.querySelectorAll('[data-nav]').forEach(e=>{if(e.dataset.nav===view)e.setAttribute('aria-current','page');else e.removeAttribute('aria-current');});if(view==='tasks')loadTasks();if(view==='experiments'&&state.draft)$('launch-config').scrollIntoView({block:'center'});else window.scrollTo(0,0);}
window.addEventListener('hashchange',applyRoute);
$('open-experiment').onclick=()=>{if(!state.overview.models.profiles.length){location.href='/settings';return;}if(!state.overview.versions.length){openVersion();return;}startExperiment(selectedProject()?.current_version||$('baseline').value);};
$('new-experiment').onclick=()=>startExperiment($('baseline').value);
document.addEventListener('click',event=>{
  const trial=event.target.closest('[data-trial]');if(trial){$('run-select').value=trial.dataset.trial;renderEvidence();reveal('evidence');return;}
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
function renderExample(data) {
  const ex=data.experiment,dev=ex.runs.filter(r=>r.phase==='development'),candidateRuns=ex.runs.filter(r=>r.role==='candidate');
  const evaluation=ex.evaluation,decision=ex.decision,loaded=candidateRuns.filter(r=>r.version_load?.verified&&r.input_registration_verified).length;
  const failureLabels={http_error:'供应商 HTTP 错误',transport_error:'模型连接或读取失败',timeout:'模型响应超时',missing_usage:'模型响应未报告用量',response_failed:'模型回复以失败状态结束',response_incomplete:'模型回复未完成'};
  const failures=[...new Set((data.failures||[]).map(r=>(failureLabels[r.category]||'模型响应异常')+(r.http_status?' '+r.http_status:'')))];
  const studyLabels={not_started:'未启动',completed:'已完成',stopped_incomplete:'未完成后停止'};
  $('example-body').innerHTML=`<p class="evo-pill">真实执行 · ${esc(ex.model)} · ${esc(ex.runner||'未记录运行器')} · 已脱敏</p>
    <h3>${esc(statusText(ex))}</h3><p>${esc(timeText(ex.created))} · ${esc(ex.id)}。这份回放不加入你的实验、预算或推荐版本。</p>
    <ol><li>开发基线：${dev.filter(r=>r.status==='passed').length}/${dev.length} 通过。</li>
    <li>${ex.candidate?`已生成候选，修改 <code>${esc(ex.target||'见文件差异')}</code>。`:'尚未生成候选。'}</li>
    <li>候选运行 ${candidateRuns.length} 次，其中 ${loaded} 次挂载哈希与输入注册证据完整。${evaluation?`配对验证取得 ${evaluation.valid_trials}/${evaluation.planned_trials} 次有效结果。`:''}</li>
    <li>${decision?`${esc(decisions[decision.status]||decision.status)}：${esc(reasons[decision.reason]||decision.reason)}。`:'尚无采用判定。'}${failures.length?`记录到：${esc(failures.join('；'))}。`:''}</li></ol>
    <p>总请求 ${ex.budget.calls} · 已知 Token ${number(ex.budget.tokens)} · 用量未知 ${ex.budget.unknown_usage_calls} 次 · 待结算 ${ex.budget.pending} 次。</p>
    ${data.study?`<p>反馈对照：${esc(studyLabels[data.study.control_status]||data.study.control_status)} · 封存报告：${esc(studyLabels[data.study.sealed_status]||data.study.sealed_status)}${data.study.recommendation_changed===false?' · 原推荐版本保留':''}。</p>`:''}
    ${ex.diff?`<details><summary>查看候选的实际文件改动</summary><pre class="evo-diff">${esc(ex.diff)}</pre></details>`:''}
    <details><summary>按执行顺序查看结果</summary><ol>${ex.runs.map(r=>`<li>${esc(r.phase==='development'?'开发':'验证')} · ${esc(r.role==='baseline'?'基线':'候选')} · ${esc(r.task)} · ${esc(labels[r.status]||r.status)}</li>`).join('')}</ol></details>
    <p>来源：${esc(data.source_document)} · ${esc(data.source_commit.slice(0,12))}</p>
    <p>${evaluation?.complete?'这份判定仅适用于本次冻结任务集与协议；泛化和反馈贡献需要另看封存与对照报告。':'有效验证尚未完整，不能推导为候选有收益或完整协议通过。'}</p>`;
}
$('show-example').onclick=async()=>{try{renderExample(await api('/api/evo/example'));$('example-dialog').showModal();}catch(error){notice(error.message);}};
async function loadTasks(){try{if(!tasks)tasks=await api('/api/evo/taskpacks');options($('taskpack-select'),tasks.packs.map(p=>[p.id,`${p.id===tasks.active?'当前可执行':'历史记录'} · ${p.id.slice(-12)}`]),$('taskpack-select').value||tasks.active);renderTasks();}catch(error){notice(error.message);}}
function renderTasks(){const pack=tasks.packs.find(p=>p.id===$('taskpack-select').value),current=pack.id===tasks.active;$('taskpack-state').textContent=current?'新实验使用此冻结任务包':'历史包仅查看；新实验使用当前执行代码';$('taskpack-id').textContent=pack.id;$('calibrate').disabled=!current;const descriptions={receipt:['结果不确定时，确认订单恰好创建一次。','验收：最终订单数量、可信状态观察与重复写入。'],listing:['从分页与延迟索引中，核对完整、一致的订单清单。','验收：客户、订单状态、最新修订、ID 集合与金额。'],workflow:['库存与确认部分成功时，正确完成或补偿整条业务流程。','验收：占用数量、确认状态、取消依据与库存释放。']};$('task-families').innerHTML=Object.entries(families).map(([key,label],i)=>`<article class="evo-task-card"><span>0${i+1} / ${esc(key.toUpperCase())}</span><h2>${esc(label)}</h2><p>${descriptions[key][0]}</p><b>${descriptions[key][1]}</b><p>开发 2 · 验证 2 · 封存 2</p></article>`).join('');$('task-cases').innerHTML=[['development','开发集','用于观察执行反馈与提出候选'],['validation','验证集','用于同题配对与准入判定'],['sealed','封存集','只用于最终报告，不回流候选生成']].map(([key,label,hint])=>`<details ${key==='development'?'open':''}><summary>${label} · ${pack.cases.filter(c=>c.split===key).length} 题 <span class="evo-muted"> / ${hint}</span></summary><ul>${pack.cases.filter(c=>c.split===key).map(c=>`<li><span>${esc(c.title)} · ${esc(c.id)}</span><code>${esc(c.checksum.slice(0,16))}</code></li>`).join('')}</ul></details>`).join('');}
$('taskpack-select').onchange=renderTasks;
$('calibrate').onclick=async()=>{$('calibrate').disabled=true;$('calibration-result').textContent='正在检查正确解、空操作与错误解…';try{const r=await api('/api/evo/calibrate',{});$('calibration-result').textContent=`${r.passed?'✓ 校准通过':'× 校准未通过'}：正确解 ${r.correct_accepted}/${r.cases} 接受；空操作 ${r.empty_rejected}/${r.cases} 拒绝；错误解 ${r.bad_rejected}/${r.cases} 拒绝。模型调用 ${r.model_calls} 次；这不是 Agent 成绩。`;}catch(error){$('calibration-result').textContent=error.message;}finally{$('calibrate').disabled=false;}};
const stageLabels={full:'完整改进',diagnose:'分析执行反馈',propose:'生成候选',validate:'评测候选'};
function selectedProject(){return state.overview?.projects.find(p=>p.id===state.project);}
function projectExperiments(){return state.overview.experiments.filter(e=>!state.project||e.project===state.project);}
function workspaceChanged(){state.project=$('workspace').value;localStorage.setItem('evo.project',state.project);state.selected=null;state.experiment=null;state.overviewKey=null;state.loadedId=null;state.experimentKey=null;state.draft=false;refresh();}
function renderWorkspace(){
  if(state.project===undefined)state.project=localStorage.getItem('evo.project')||'';
  if(!state.overview.projects.some(p=>p.id===state.project))state.project='';
  options($('workspace'),[['','全部项目'],...state.overview.projects.map(p=>[p.id,p.name])],state.project);
  const p=selectedProject(),wasOpen=$('workspace-details')?.open;
  $('workspace-summary').hidden=!p;
  $('workspace-summary').innerHTML=p?`<details id="workspace-details" class="evo-inline-details" ${wasOpen?'open':''}><summary>项目详情${p.cost.unknown_usage_calls?` · ${p.cost.unknown_usage_calls} 次用量未知`:''}</summary><p>${esc(p.goal)}</p><p>${esc(p.runner)} · ${esc(p.model)} · ${esc(p.target)}</p><p>${compactNumber(p.cost.tokens)} 已知 Token · ${p.cost.calls} 次请求${p.cost.unknown_usage_calls?` · ${p.cost.unknown_usage_calls} 次用量未知`:''}</p>${p.imported_from?'<p>从交付包导入，需重新验证。</p>':''}</details>`:'';
  $('new-project').disabled=!state.overview.versions.length;$('export-project').hidden=!p;if(p)$('export-project').href='/api/evo/projects/'+encodeURIComponent(p.id)+'/bundle';
}
function stageChanged(resetBudget=true){
  const automatic=$('protocol').value==='improve',stage=$('improve-stage').value;
  $('stage-config').hidden=!automatic;
  $('protocol-hint').hidden=automatic&&stage!=='full';
  if(automatic)options($('candidate'),[['',{full:'运行开发集后由模型生成',diagnose:'本阶段只保存诊断',propose:'从已有诊断生成候选',validate:'使用来源中已保存的候选'}[stage]]]);
  const p=selectedProject();
  if(p&&automatic){$('baseline').value=p.current_version;$('feedback-mode').value=p.feedback_mode||'development';if(p.target)$('target').value=p.target;}
  $('baseline').disabled=Boolean(p&&automatic);$('feedback-mode').disabled=Boolean(p&&automatic);$('target').disabled=Boolean(p&&automatic);
  const sources=projectExperiments().filter(e=>e.baseline===$('baseline').value&&e.target===$('target').value&&!active(e.status)&&e.status!=='interrupted'&&(stage!=='validate'||e.candidate));
  options($('stage-source'),[['',stage==='diagnose'?'重新运行开发任务':'选择同项目的来源产物'],...sources.map(e=>[e.id,`${e.id.slice(-6)} · ${stageLabels[e.stage]||'完整改进'} · ${statusText(e)}`])],$('stage-source').value);
  $('stage-source-label').hidden=stage==='full';
  const hints={full:'运行开发基线 → 诊断 → 生成 → 24 次配对验证。',diagnose:'运行开发任务，保存诊断与假设。',propose:'复用诊断，生成一个候选。',validate:'24 次配对验证，通过后更新推荐。'};
  $('stage-hint').textContent=hints[stage];
  if(resetBudget&&automatic){const limits=stage==='propose'?{calls:1,tokens:100000,seconds:600}:stage==='diagnose'?{calls:74,tokens:700000,seconds:3000}:stage==='validate'?state.overview.protocols.validation.limits:state.overview.protocols.improve.limits;for(const k of ['calls','tokens','seconds'])$('budget-'+k).value=limits[k];}
  $('run-experiment').textContent=automatic?'开始'+stageLabels[stage]:'验证两个版本';
}
function openProject(){
  $('project-form').reset();options($('project-baseline'),state.overview.versions.map(v=>[v.id,v.name]));projectBaselineChanged();$('project-error').textContent='';$('project-dialog').showModal();
}
function projectBaselineChanged(){const v=version($('project-baseline').value);options($('project-target'),Object.keys(v?.manifest.files||{}).map(k=>[k,k]));}
function reveal(id){const element=$(id);if(element.tagName==='DETAILS')element.open=true;element.scrollIntoView({behavior:'smooth',block:'start'});}
function renderJourney(){
  const ex=state.experiment;if(!ex)return;
  const p=state.overview.projects.find(p=>p.id===ex.project);
  const next=p?.current_version!==ex.baseline?null:ex.phase==='diagnosed'?'propose':ex.phase==='candidate_ready'?'validate':null;
  $('comparison-summary').innerHTML=`<div class="evo-context-actions"><span>${esc(ex.target||shortMode(ex))}</span><div class="evo-inline">${ex.diagnosis?'<button data-scroll="reasoning">改进理由 ↓</button>':''}${ex.source?`<button data-experiment="${esc(ex.source)}">来源实验 ↗</button>`:''}${next?`<button class="primary" data-next-stage="${next}" data-source="${esc(ex.id)}">${stageLabels[next]} →</button>`:''}</div></div>`;
}
$('workspace').onchange=workspaceChanged;$('new-project').onclick=openProject;$('project-baseline').onchange=projectBaselineChanged;
$('improve-stage').onchange=()=>stageChanged();$('target').onchange=()=>stageChanged(false);
$('project-form').onsubmit=async event=>{event.preventDefault();$('project-submit').disabled=true;try{const p=await api('/api/evo/projects',{name:$('project-name').value,goal:$('project-goal').value,baseline:$('project-baseline').value,target:$('project-target').value,feedback_mode:$('project-feedback').value},crypto.randomUUID());$('project-dialog').close();state.project=p.id;localStorage.setItem('evo.project',p.id);state.selected=null;state.experiment=null;state.overviewKey=null;state.loadedId=null;state.experimentKey=null;state.draft=false;await refresh();notice('项目已创建，初始版本和任务协议已固定。');}catch(error){$('project-error').textContent=error.message;}finally{$('project-submit').disabled=false;}};
document.addEventListener('click',event=>{const scroll=event.target.closest('[data-scroll]');if(scroll)reveal(scroll.dataset.scroll);const next=event.target.closest('[data-next-stage]');if(next){const ex=state.overview.experiments.find(e=>e.id===next.dataset.source);state.project=ex.project;localStorage.setItem('evo.project',state.project);renderWorkspace();startExperiment(ex.baseline);$('improve-stage').value=next.dataset.nextStage;stageChanged();$('stage-source').value=ex.id;}});

const campaignActive = status => ['queued','running','cancelling'].includes(status);
const campaignStatuses = {queued:'等待开始',running:'持续改进中',cancelling:'正在停止',cancelled:'已取消',completed:'计划完成',stopped:'已停止',interrupted:'进程中断'};
const campaignReasons = {...reasons,round_limit:'达到设定轮数',no_gain_limit:'连续无收益，保留当前推荐',user_cancelled:'用户停止计划',owner_process_lost:'执行进程丢失，未自动继续',execution_incomplete:'本轮证据未完整收集',execution_interrupted:'执行已中断',model_changed:'模型配置发生变化',controller_error:'控制器异常'};
function renderCampaigns(){
  const p=selectedProject(),plans=(state.overview.campaigns||[]).filter(c=>c.project===p?.id);
  $('campaign-panel').hidden=!p||(!p.explicit_workspace&&!plans.length);if(!p)return;
  $('new-campaign').disabled=!p.explicit_workspace||!p.task_pack_current||!state.overview.ready||state.overview.experiments.some(e=>active(e.status))||state.overview.campaigns.some(c=>campaignActive(c.status));
  $('new-campaign').title=p.explicit_workspace?'':'请从当前版本创建项目';
  const expanded=new Set([...$('campaign-list').querySelectorAll('details[open]')].map(d=>d.id));
  $('campaign-list').innerHTML=plans.length?plans.map(c=>`<article class="evo-campaign"><div class="evo-campaign-line"><b>${esc(campaignStatuses[c.status]||c.status)}</b><span>${c.experiments.length}/${c.max_rounds} 轮</span><small>${esc(campaignReasons[c.reason]||c.reason||'共享预算执行中')}</small>${campaignActive(c.status)?`<button data-cancel-campaign="${esc(c.id)}" ${c.status==='cancelling'?'disabled':''}>停止</button>`:''}</div>${c.budget?.unknown_usage_calls?`<p class="evo-error">${c.budget.unknown_usage_calls} 次用量未知</p>`:''}<details class="evo-inline-details" id="details-${esc(c.id)}" ${expanded.has('details-'+c.id)?'open':''}><summary>轮次与用量</summary><div class="evo-campaign-rounds">${c.experiments.map((id,i)=>{const ex=state.overview.experiments.find(e=>e.id===id);return `<button data-experiment="${esc(id)}">第 ${i+1} 轮 · ${esc(ex?statusText(ex):'等待执行')} ↗</button>`;}).join('')}</div><p>请求 ${c.budget?.calls||0}/${c.limits.calls} · 已知 Token ${number(c.budget?.tokens||0)}/${number(c.limits.tokens)} · ${c.limits.seconds}s</p><p>连续无收益 ${c.no_gain_count}/${c.no_gain_limit} · ${esc(timeText(c.created))}</p></details></article>`).join(''):'<p class="evo-scope">尚无多轮计划</p>';
}
let campaignKey=null;
$('new-campaign').onclick=()=>{const p=selectedProject();if(!p)return;$('campaign-form').reset();campaignKey=crypto.randomUUID();$('campaign-context').textContent=`${p.name} · 从 ${version(p.current_version)?.name} 开始 · 修改 ${p.target}`;options($('campaign-optimizer'),state.overview.models.profiles.map(m=>[m.id,`${m.name} · ${m.model}`]),version(p.current_version)?.manifest.model.profile);$('campaign-error').textContent='';$('campaign-dialog').showModal();};
$('campaign-form').onsubmit=async event=>{event.preventDefault();$('campaign-submit').disabled=true;try{const p=selectedProject();await api('/api/evo/campaigns',{project:p.id,max_rounds:Number($('campaign-rounds').value),no_gain_limit:Number($('campaign-no-gain').value),optimizer_profile:$('campaign-optimizer').value,limits:Object.fromEntries(['calls','tokens','seconds'].map(k=>[k,Number($('campaign-'+k).value)]))},campaignKey);$('campaign-dialog').close();notice('多轮计划已启动，所有轮次共用一份总预算。');await refresh();}catch(error){$('campaign-error').textContent=error.message;}finally{$('campaign-submit').disabled=false;}};
$('campaign-list').addEventListener('click',async event=>{const b=event.target.closest('[data-cancel-campaign]');if(!b)return;b.disabled=true;try{await api('/api/evo/campaigns/'+encodeURIComponent(b.dataset.cancelCampaign)+'/cancel',{});notice('已停止派发新请求；已发出的调用仍会结算并保留证据。');await refresh();}catch(error){notice(error.message);b.disabled=false;}});

applyRoute();refresh();setInterval(()=>{if(!document.querySelector('dialog[open]')&&!state.busy)refresh();},4000);
