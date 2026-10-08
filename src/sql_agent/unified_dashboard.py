"""PeopleOps Copilot's internal operations workspace."""


def render_unified_dashboard():
    return '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PeopleOps Copilot</title><style>body{font:16px system-ui;max-width:1000px;margin:2rem auto;padding:0 1rem;color:#183342;background:#f5f8fa}.card{background:white;padding:1.2rem;margin:1rem 0;border:1px solid #d8e2e8;border-radius:10px}label{display:block;margin:.7rem 0}input,select,textarea{font:inherit;width:100%;padding:.65rem;box-sizing:border-box}button{font:inherit;padding:.65rem 1rem;margin:.7rem .4rem 0 0;cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:500px;overflow:auto;background:#f0f5f7;padding:1rem}[hidden]{display:none!important}.muted{color:#5b6e7a}#feedback{padding:.8rem;background:#eaf3f6}</style>
<h1>PeopleOps Copilot</h1><p>Employee policy, workforce data, and reviewed HR actions in one operations workspace.</p>
<p class="muted">Policy answers include their source. Employee data and actions follow your assigned role. Leave changes wait for manager approval.</p>
<p id="feedback" role="status">Sign in to begin.</p>
<section id="login-panel" class="card"><label>Access key<input id="key" type="password" autocomplete="off"></label><button id="login">Sign in</button></section>
<main id="workspace" hidden><p><span id="identity"></span> <button id="logout">Sign out</button></p>
<section class="card"><h2>People operations</h2>
<label>Policy question<input id="policy-question" placeholder="What is the annual leave policy?"></label><button id="policy-ask">Search policies</button>
<label>Department headcount<input id="department" placeholder="Engineering (leave blank for all)"></label><button id="headcount-ask">Get headcount</button>
<label>Employee code<input id="employee-code" placeholder="E002"></label><button id="profile-read">Profile</button><button id="balance-read">Leave balance</button><button id="attendance-read">Attendance</button>
<h3>Request annual leave</h3><label>Start date<input id="leave-start" type="date"></label><label>End date<input id="leave-end" type="date"></label><label>Reason<input id="leave-reason" maxlength="500"></label><button id="leave-create">Preview and request approval</button>
<h3>Pending approvals</h3><button id="approval-load">Refresh approvals</button><label>Approval ID<input id="approval-id"></label><button id="leave-approve">Approve leave</button><button id="leave-reject">Reject leave</button><pre id="peopleops-result" aria-live="polite"></pre></section>
<details><summary>Advanced registered SQL workflow</summary>
<section class="card"><label>Source<select id="source"></select></label><pre id="source-info"></pre>
<label>Input<select id="input-kind"><option value="question">Natural-language question</option><option value="sql">SQL statement</option></select></label>
<label>Request<textarea id="prompt" rows="5" placeholder="Run a configured, read-only workforce analytics task"></textarea></label>
<button id="submit">Run request</button><p class="muted">The API queues work and returns a request ID. No mutation is committed by submitting a request.</p></section>
<section class="card"><label>Request ID<input id="request-id" autocomplete="off"></label><button id="load">Open request</button>
<h2 id="status">No request selected</h2><pre id="result"></pre>
<h3>Token cost</h3><p id="token-cost">No usage observed.</p>
<details><summary>Planner and Verifier usage</summary><pre id="usage"></pre></details>
<div id="clarification-panel" hidden><label>Clarification requested<span id="clarification-question"></span><textarea id="clarification-answer" rows="3"></textarea></label><button id="answer">Submit answer</button></div>
<details><summary>Business definitions</summary><pre id="definitions"></pre></details>
<details><summary>Source health</summary><pre id="health"></pre></details>
<details><summary>Retrieved knowledge (RAG)</summary><pre id="retrieval"></pre></details>
<label>Review notes<input id="notes"></label>
<button id="approve" disabled>Approve</button><button id="reject" disabled>Reject</button></section></details></main>
<script>
const el=id=>document.getElementById(id);let sources=[],role='',job=null,timer=null,busy=false,epoch=0,pending=null;
function feedback(s){el('feedback').textContent=s;}
function controls(){el('submit').disabled=busy||role==='viewer';el('answer').disabled=busy||role==='viewer'||job?.status!=='waiting_user';for(const id of ['approve','reject'])el(id).disabled=busy||role!=='admin'||!job||job.status!=='needs_review';}
function clear(){epoch++;clearTimeout(timer);job=null;pending=null;role='';el('workspace').hidden=true;el('login-panel').hidden=false;el('result').textContent='';el('request-id').value='';el('prompt').value='';el('key').value='';el('usage').textContent='';el('token-cost').textContent='No usage observed.';el('clarification-answer').value='';el('clarification-panel').hidden=true;controls();}
async function api(path,payload,headers={}){const r=await fetch(path,{method:payload?'POST':'GET',headers:{'Content-Type':'application/json',...headers},body:payload?JSON.stringify(payload):undefined});const v=await r.json();if(!r.ok){if(r.status===401)clear();throw Error(typeof v.detail==='string'?v.detail:JSON.stringify(v.detail));}return v;}
async function run(fn){if(busy)return;busy=true;controls();try{await fn();}catch(e){feedback(e.message);}finally{busy=false;controls();}}
function choose(){const s=sources[el('source').selectedIndex];el('source-info').textContent=JSON.stringify(s,null,2);if(s?.task_id&&el('input-kind').value==='question')el('prompt').value=s.question;pending=null;}
async function session(){const s=await api('/v1/auth/session');role=s.role;el('identity').textContent=s.name+' ('+s.role+')';const list=await api('/v1/sources');sources=[...list.tasks,...list.databases];el('source').replaceChildren();for(const s of sources){const o=document.createElement('option');o.textContent=s.task_id?s.task_id+' · contracted analysis':s.database_id+' · '+s.engine;o.value=s.task_id||s.database_id;el('source').append(o);}el('login-panel').hidden=true;el('workspace').hidden=false;choose();feedback('Ready.');controls();}
function show(value){if(job?.job_id!==value.job_id){el('notes').value='';el('clarification-answer').value='';}job=value;el('request-id').value=value.job_id;el('status').textContent=value.status;el('result').textContent=JSON.stringify(value,null,2);el('definitions').textContent=JSON.stringify(value.result?.business_context?.definitions||[],null,2);el('health').textContent=JSON.stringify(value.result?.source_health||{status:'not_configured_for_source'},null,2);const evidence=value.result||value.error?.evidence||{};el('retrieval').textContent=JSON.stringify(evidence.retrieval||{status:'not_requested'},null,2);const usage=evidence.telemetry;const cost=usage?.token_cost;el('usage').textContent=JSON.stringify(usage||{},null,2);el('token-cost').textContent=cost?(cost.complete?cost.input_tokens+' input + '+cost.output_tokens+' output = '+cost.total_tokens+' tokens (including recorded retries).':'Incomplete usage: '+cost.observed_total_tokens+' tokens observed; total unknown.'):'No usage observed.';el('clarification-panel').hidden=value.status!=='waiting_user';el('clarification-question').textContent=value.result?.clarification?.question||'';controls();}
async function poll(id,stamp){if(stamp!==epoch)return;try{const r=await api('/v1/requests/'+encodeURIComponent(id));if(stamp!==epoch)return;show(r);if(['queued','running'].includes(r.status))timer=setTimeout(()=>poll(id,stamp),700);}catch(e){feedback(e.message);}}
el('login').onclick=()=>run(async()=>{await api('/v1/auth/login',{api_key:el('key').value});el('key').value='';await session();});
el('logout').onclick=()=>run(async()=>{await api('/v1/auth/logout',{});clear();feedback('Signed out.');});
el('policy-ask').onclick=()=>run(async()=>{const result=await api('/agent/query/policy',{question:el('policy-question').value});el('peopleops-result').textContent=JSON.stringify(result,null,2);});
el('headcount-ask').onclick=()=>run(async()=>{const department=el('department').value.trim();const result=await api('/agent/query/headcount',department?{department}:{});el('peopleops-result').textContent=JSON.stringify(result,null,2);});
for(const [button,suffix] of [['profile-read',''],['balance-read','/leave-balance'],['attendance-read','/attendance']])el(button).onclick=()=>run(async()=>{const code=el('employee-code').value.trim();const result=await api('/employees/'+encodeURIComponent(code)+suffix);el('peopleops-result').textContent=JSON.stringify(result,null,2);});
el('leave-create').onclick=()=>run(async()=>{const result=await api('/agent/actions/leave-requests',{employee_code:el('employee-code').value.trim(),leave_type:'annual',start_date:el('leave-start').value,end_date:el('leave-end').value,reason:el('leave-reason').value});el('peopleops-result').textContent=JSON.stringify(result,null,2);});
el('approval-load').onclick=()=>run(async()=>{const result=await api('/approvals/pending');el('peopleops-result').textContent=JSON.stringify(result,null,2);});
for(const [button,decision] of [['leave-approve','approve'],['leave-reject','reject']])el(button).onclick=()=>run(async()=>{const id=el('approval-id').value.trim();const result=await api('/approvals/'+encodeURIComponent(id)+'/review',{decision});el('peopleops-result').textContent=JSON.stringify(result,null,2);});
el('source').onchange=choose;el('input-kind').onchange=choose;el('prompt').oninput=()=>{pending=null;};
el('request-id').oninput=()=>{epoch++;clearTimeout(timer);job=null;controls();};
el('submit').onclick=()=>run(async()=>{const s=sources[el('source').selectedIndex];if(!s)throw Error('Select a source');const p=s.task_id?{task_id:s.task_id}:{database_id:s.database_id};p[el('input-kind').value]=el('prompt').value;const signature=JSON.stringify(p);if(!pending||pending.signature!==signature)pending={signature,key:crypto.randomUUID()};const r=await api('/v1/requests',p,{'idempotency-key':pending.key});epoch++;clearTimeout(timer);show(r.job);feedback('Submitted. Inspect the result before review.');poll(r.job.job_id,epoch);});
el('load').onclick=()=>run(async()=>{epoch++;clearTimeout(timer);await poll(el('request-id').value,epoch);});
el('answer').onclick=()=>run(async()=>{const current=job;if(current?.status!=='waiting_user')return;const answer=el('clarification-answer').value.trim();if(!answer)throw Error('Enter an answer');const p={clarification_id:current.result.clarification.id,answer};const signature=JSON.stringify(p);if(!pending||pending.signature!==signature)pending={signature,key:crypto.randomUUID()};await api('/v1/requests/'+encodeURIComponent(current.job_id)+'/resume',p,{'idempotency-key':pending.key});epoch++;await poll(current.job_id,epoch);});
for(const decision of ['approve','reject'])el(decision).onclick=()=>run(async()=>{const current=job;if(!current)return;const proposal=current.result?.proposal;const payload={decision,notes:el('notes').value};if(proposal)payload.proposal_sha256=proposal.proposal_sha256;if(decision==='approve'&&!confirm(proposal?'Execute this exact change?\\n'+proposal.sql:'Approve this analysis result?'))return;await api('/v1/requests/'+encodeURIComponent(current.job_id)+'/review',payload);await poll(current.job_id,epoch);});
session().catch(()=>feedback('Sign in to begin.'));
</script></html>'''
