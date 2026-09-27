const $ = id => document.getElementById(id);
// $ takes an ID, q takes a selector. Three call sites passed a selector to $, which returns null
// for one: the first addEventListener on it threw out of init() before a single form, nav button,
// refresh timer or session resume was bound, so the whole console was inert and login fell back to
// a native form submit. test_ui_assets.py only greps this file for substrings and the Playwright
// spec needs npm and a live server, so nothing in the pytest suite noticed.
const q = selector => document.querySelector(selector);
const S = {csrf:null,project:null,epoch:0,conversationEpoch:0,view:"overview",rooms:[],roomOlder:null,agents:[],caps:[],catalog:[],catalogOlder:null,catalogPaged:false,capabilityEditBase:null,pendingAgents:[],pendingOlder:null,pendingPaged:false,pendingAgentTags:[],roomAgentTags:null,roomAgentKey:null,roomAgentUpdatedAt:null,roomConfigUpdatedAt:null,roomConfigParallel:20,roomConfigAuto:true,roomConfigDraft:null,roomChoiceDraft:null,tasks:[],taskCounts:null,assignmentTasks:[],assignmentOlder:null,expandedTasks:new Set(),expandedMessages:new Set(),candidates:[],candidateTask:null,candidateOlder:null,candidateRequest:0,candidateMemberCount:0,instructions:[],olderCursor:null,taskOlder:null,instructionOlder:null,agentOlder:null,latestDisplayedSeq:null,hasHiddenUnseen:false};
const headings = {
  overview:["Overview","YOUR WORKSPACE, AT A GLANCE","The conversations and work moving through your project."],
  rooms:["Rooms","CONVERSATIONS BY TOPIC","Create focused spaces and put together the right team."],
  capabilities:["Capabilities","PROJECT SKILL CATALOG","Define project capabilities and review legacy assignments."],
  agents:["Agents","PEOPLE & PRESENCE","See who is around, where they are working, and what they can do."],
  tasks:["Tasks","GRAPH-BACKED WORK","Assign work, track its status, and keep a clear owner."],
  instructions:["Instructions","HUMAN DIRECTION","Durable requests agents receive when they check in."],
  messages:["Messages","PROJECT HISTORY","Read recent room conversations and direct messages."]
};
function make(tag,cls,value) {
  const n=document.createElement(tag); if(cls)n.className=cls;
  if(value!==undefined)n.textContent=String(value); return n;
}
const address = form => ["user","device","client"].map(x=>form.querySelector('[name="address_'+x+'"]').value.trim());
const label = a => Array.isArray(a)?a.join(" · "):"Unassigned";
function openAgentDm(agent){
  const key=JSON.stringify(agent);
  $("dm-destination").value="agent";updateMessageDestination();
  const select=$("dm-recipient");
  if(![...select.options].some(option=>option.value===key)){
    const option=make("option","",label(agent));option.value=key;select.append(option);
  }
  select.value=key;
  switchView("agents");
  $("dm-form").scrollIntoView?.({behavior:"smooth",block:"center"});
  q('#dm-form [name="body"]')?.focus?.();
}
function openAgentSettings(agent){
  const key=JSON.stringify(agent),select=$("capability-agent");
  if(![...select.options].some(option=>option.value===key)){
    const option=make("option","",label(agent));option.value=key;select.append(option);
  }
  select.value=key;S.roomAgentKey=null;S.roomAgentTags=null;S.pendingAgentTags=[];
  switchView("agents");loadAgentCapabilities();
  $("agent-config-form").scrollIntoView?.({behavior:"smooth",block:"center"});
}
function humanMessage(destination, recipient, room, body, key) {
  if(destination==="room")return {tail:"messages/room",data:{room,body,idempotency_key:key}};
  if(destination==="agent")return {tail:"dm",data:{address:JSON.parse(recipient),body,
                                                     idempotency_key:key}};
  throw Error("Choose an agent or a room.");
}
const when = t => t?new Date(t*1000).toLocaleString():"Not seen yet";
const relative = t => !t?"not seen":Math.max(0,Math.round((Date.now()/1000-t)/60))+" min ago";
function notice(msg,bad=false) { const n=$("notice"); n.textContent=msg; n.className=bad?"notice error":"notice"; n.hidden=!msg; }
function showVersion(session){
  $("server-version").textContent=session.version?"v"+session.version:"Version unavailable";
}
function empty(msg) { return make("p","empty",msg); }
function item(title,detail,symbol,status,symbolClass="") {
  const card=make("article","item-card"),top=make("div","item-top"),head=make("div","row-title");
  head.append(make("span","symbol "+symbolClass,symbol),make("h3","",title));top.append(head);
  if(status)top.append(make("span","chip "+status.replaceAll(" ","_"),status));
  card.append(top);if(detail)card.append(make("p","",detail));return card;
}
function expandedDetail(card, body, id, opened, name, preview) {
  const detail=make("details","expandable"),content=String(body||"");
  const short=Array.from(String(preview||content)),max=preview?160:96;
  const summary=make("summary","preview",short.slice(0,max).join("")+
    (short.length>max?"…":""));
  summary.setAttribute("aria-label","Expand full "+name);
  detail.append(summary,make("p","body",content));
  detail.open=opened.has(id);
  detail.addEventListener("toggle",()=>{
    if(detail.open)opened.add(id);else opened.delete(id);
  });
  card.append(detail);
  return detail;
}
async function api(path,opts={}) {
  const epoch=S.epoch;
  const r=await fetch(path,{credentials:"same-origin",headers:{"Content-Type":"application/json",
    ...(opts.method==="POST"?{"X-CSRF-Token":S.csrf}:{})},...opts});
  const body=await r.json();
  if(!r.ok){if(r.status===401&&path!=="/api/login"&&path!=="/api/session"&&
     epoch===S.epoch)showLogin();throw Error(body.error||"Request failed");}
  return body;
}
const url=tail=>"/api/projects/"+encodeURIComponent(S.project)+"/"+tail;
function resetProjectState(){
  S.rooms=[];S.roomOlder=null;S.agents=[];S.agentOlder=null;S.caps=[];
  S.catalog=[];S.catalogOlder=null;S.catalogPaged=false;S.capabilityEditBase=null;S.pendingAgents=[];S.pendingOlder=null;
  S.pendingPaged=false;S.pendingAgentTags=[];S.roomAgentTags=null;S.roomAgentKey=null;
  S.roomAgentUpdatedAt=null;S.roomConfigUpdatedAt=null;S.roomConfigDraft=null;
  S.roomConfigParallel=20;S.roomConfigAuto=true;S.roomChoiceDraft=null;
  S.tasks=[];S.taskCounts=null;S.assignmentTasks=[];S.assignmentOlder=null;
  S.expandedTasks.clear();S.expandedMessages.clear();
  S.taskOlder=null;S.candidates=[];S.candidateTask=null;
  S.candidateOlder=null;S.candidateMemberCount=0;S.candidateRequest++;
  S.instructions=[];S.instructionOlder=null;S.olderCursor=null;
  S.latestDisplayedSeq=null;S.hasHiddenUnseen=false;S.conversationEpoch++;
  for(const node of document.querySelectorAll("[data-room-select]"))node.replaceChildren();
  for(const draft of document.querySelectorAll("#main form"))draft.reset();
  $("instruction-agent-field").hidden=false;
  $("instruction-recipient").required=true;
  for(const id of ["task-select","assignee-select","dm-recipient","instruction-recipient"])
    $(id).replaceChildren();
  for(const id of ["room-list","overview-rooms","agent-list","overview-agents",
    "task-list","task-counts","instruction-list","message-list","message-room-info",
    "capability-catalog","capability-choices","agent-pending-choices","pending-agent-list"])
    $(id).replaceChildren();
  for(const id of ["stat-rooms","stat-agents","stat-tasks","stat-queue"])
    $(id).textContent="—";
  $("breadcrumb-project").textContent="PROJECT";
  $("refreshed").textContent="";notice("");
  $("message-unread").hidden=true;$("message-unread").textContent="";
  $("message-channel").value="room";
  $("dm-destination").value="agent";updateMessageDestination();
  $("task-status-filter").value="all";
  switchView("overview");
}
function showLogin(){S.csrf=null;S.project=null;S.epoch++;resetProjectState();
  $("project-switcher").replaceChildren();
  $("login-screen").hidden=false;$("app-shell").hidden=true;$("login-token").value="";}
function switchView(v) {
  if(!headings[v])return;S.view=v;
  for(const panel of document.querySelectorAll(".view"))panel.hidden=panel.id!=="view-"+v;
  for(const nav of document.querySelectorAll("[data-view]"))nav.classList.toggle("selected",nav.dataset.view===v);
  [$("page-title"),$("page-kicker"),$("page-subtitle")].forEach((n,i)=>n.textContent=headings[v][i]);
  if(v==="messages")messages();
}
function selectOptions(node,values,optional=false,preserveUnknown=false){
  const old=node.value,options=[];
  if(optional){const x=make("option","","No room");x.value="";options.push(x);}
  for(const value of values){const x=make("option","",value);x.value=value;options.push(x);}
  if(preserveUnknown&&old&&!values.includes(old)){
    const x=make("option","",old+" (selected; rechecked on submit)");
    x.value=old;options.push(x);
  }
  node.replaceChildren(...options);
  if(values.includes(old)||(preserveUnknown&&old))node.value=old;
}
function renderRooms(){
  const render=r=>{
    const n=item("# "+r.name,r.description,"▦",r.manager?"managed":"unmanaged");
    n.append(make("small","","Manager: "+label(r.manager)+" · "+r.member_count+" members"));
    const tags=make("div","metadata");
    for(const a of r.members){
      const member=make("span","chip",label(a)+" ");
      if(!["human","webui"].includes(a[2])){
        const dm=make("button","chip-remove","DM");dm.type="button";
        dm.setAttribute("aria-label","DM "+label(a));
        dm.addEventListener("click",()=>openAgentDm(a));member.append(dm);
      }
      const remove=make("button","chip-remove","×");remove.type="button";
      remove.setAttribute("aria-label","Remove "+label(a)+" from "+r.name);
      remove.addEventListener("click",()=>{
        if(window.confirm("Remove "+label(a)+" from #"+r.name+"?"))
          mutate("rooms/"+encodeURIComponent(r.name)+"/members/remove",
            {address:a},"Agent removed from room.");
      });
      member.append(remove);tags.append(member);
    }
    n.append(tags);
    if(r.members_older_cursor){
      const more=make("button","text-button","Load more members of #"+r.name);more.type="button";
      more.addEventListener("click",()=>loadRoomMembers(r));n.append(more);
    }
    return n;
  };
  $("room-list").replaceChildren(...(S.rooms.length?S.rooms.map(render):[empty("No rooms yet. Create one to give your team a place to work.")]));
  $("overview-rooms").replaceChildren(...(S.rooms.length?S.rooms.slice(0,5).map(render):[empty("Create a room to get started.")]));
  for(const n of document.querySelectorAll("[data-room-select]"))
    selectOptions(n,S.rooms.map(r=>r.name),n.hasAttribute("data-optional-room"),true);
  const r=S.rooms.find(x=>x.name===$("message-room").value);
  $("message-room-info").replaceChildren(r?render(r):empty("Choose a room to see its context."));
  $("room-older").hidden=!S.roomOlder;
}
function renderCatalog(){
  const cards=S.catalog.map(cap=>{
    const n=item(cap.name,cap.description||"Description needed (legacy tag)","◈",
      cap.approved?"approved":"pending");
    const edit=make("button","text-button",cap.approved?"Edit description":"Approve capability");
    edit.type="button";
    edit.addEventListener("click",()=>{
      q('#capability-create-form [name="name"]').value=cap.name;
      q('#capability-create-form [name="description"]').value=cap.description;
      q('#capability-create-form [name="description"]').focus?.();
    });n.append(edit);return n;
  });
  $("capability-catalog").replaceChildren(...(cards.length?cards:[empty("Add a project capability with a description.")]));
  $("capability-older").hidden=!S.catalogOlder;
}
function renderPendingAssignments(){
  const cards=S.pendingAgents.map(agent=>{
    const card=item(label(agent.address),agent.pending_capabilities.join(", "),"◎","pending");
    const review=make("button","text-button","Review agent");review.type="button";
    review.addEventListener("click",()=>openAgentSettings(agent.address));card.append(review);
    return card;
  });
  $("pending-agent-list").replaceChildren(...(cards.length?cards:
    [empty("No pending legacy assignments on this page.")]));
  $("pending-agent-older").hidden=!S.pendingOlder;
}
async function loadAgentCapabilities(){
  const key=$("capability-agent").value,epoch=S.epoch;
  if(!key)return;
  try{
    const address=JSON.parse(key),params=new URLSearchParams(
      {user:address[0],device:address[1],client:address[2]});
    const [data,grants]=await Promise.all([
      api(url("agents/config?"+params)),api(url("agents/capabilities?"+params))]);
    if(epoch!==S.epoch||key!==$("capability-agent").value)return;
    // A draft in progress keeps the revision it started from. This runs on the 20s tick while
    // the agents view is open, and overwriting the revision under an open draft defeated the
    // server's compare-and-swap: another operator's save would be absorbed silently and then
    // overwritten on submit, with no conflict raised. Holding the base revision makes the
    // server reject the stale submit, which is the whole point of sending one.
    const drafting=S.roomChoiceDraft?.key===key;
    S.roomAgentTags=grants.capabilities;
    S.pendingAgentTags=grants.pending_capabilities||[];
    if(!drafting)S.roomAgentUpdatedAt=grants.updated_at;
    S.roomConfigUpdatedAt=data.updated_at;
    S.roomConfigParallel=data.max_parallel_tasks;
    S.roomConfigAuto=data.auto_claim_enabled;
    S.roomAgentKey=key;
    renderAgentCapabilityEditor();
  }catch(e){if(epoch===S.epoch)notice(e.message,true);}
}
function renderAgentCapabilityEditor(){
  const memberSelect=$("capability-agent"),old=memberSelect.value;
  const members=S.agents.map(a=>a.address).filter(a=>!["human","webui"].includes(a[2]));
  if(old&&!members.some(a=>JSON.stringify(a)===old)){
    try{members.push(JSON.parse(old));}catch(_){/* Drop malformed old selection. */}
  }
  const options=members.map(a=>{const o=make("option","",label(a));o.value=JSON.stringify(a);return o;});
  memberSelect.replaceChildren(...options);
  memberSelect.value=options.some(o=>o.value===old)?old:(options[0]?.value||"");
  const key=memberSelect.value;
  if(S.roomAgentKey!==key){S.roomAgentTags=null;S.roomAgentUpdatedAt=null;
    S.roomConfigUpdatedAt=null;S.roomConfigDraft=null;
    S.roomAgentKey=key;S.roomChoiceDraft=null;
    S.pendingAgentTags=[];
    if(key)loadAgentCapabilities();}
  const choices=$("capability-choices");
  if(S.roomAgentTags===null){
    choices.replaceChildren(empty(key?"Loading agent settings…":"Choose a project agent."));
    $("agent-pending-choices").replaceChildren();
    q('#capability-assign-form [type="submit"]').disabled=true;
    q('#agent-config-form [type="submit"]').disabled=true;
    return;
  }
  const draft=S.roomConfigDraft?.key===key?S.roomConfigDraft:null;
  $("agent-config-parallel").value=draft?.parallel??S.roomConfigParallel;
  $("agent-config-auto-claim").checked=draft?.auto??S.roomConfigAuto;
  q('#agent-config-form [type="submit"]').disabled=false;
  const pending=new Set(S.pendingAgentTags);
  const selected=S.roomChoiceDraft?.key===key?S.roomChoiceDraft.tags:
    new Set(S.roomAgentTags.filter(tag=>!pending.has(tag)));
  choices.replaceChildren(...S.catalog.map(cap=>{
    const row=make("label","capability-choice"),input=make("input");input.type="checkbox";
    input.value=cap.name;input.checked=selected.has(cap.name)&&!!cap.approved;
    input.disabled=!cap.approved;
    input.addEventListener("change",()=>{
      if(!S.roomChoiceDraft||S.roomChoiceDraft.key!==key)
        S.roomChoiceDraft={key,tags:new Set(S.roomAgentTags.filter(tag=>!pending.has(tag)))};
      if(input.checked)S.roomChoiceDraft.tags.add(cap.name);
      else S.roomChoiceDraft.tags.delete(cap.name);
    });
    row.append(input,make("span","",cap.name+" — "+
      (cap.description||"description needed")+(pending.has(cap.name)?" · pending agent approval":"")+
      (!cap.approved?" · approve definition first":"")));
    return row;
  }));
  if(!S.catalog.length)choices.replaceChildren(empty("Create a capability to assign to agents."));
  $("agent-pending-choices").replaceChildren(...(pending.size?[
    make("p","muted","Pending legacy tags: "+[...pending].join(", ")+
      ". Check approved tags above to confirm them. Saving removes any unselected pending tags.")]:[]));
  q('#capability-assign-form [type="submit"]').disabled=false;
}
function selectedAgentCapabilities(){
  const selected=$("capability-agent").value;
  if(!selected||S.roomAgentTags===null)throw Error("Choose a project agent first.");
  const visible=new Set(S.catalog.map(cap=>cap.name));
  const current=S.roomChoiceDraft?.key===selected?S.roomChoiceDraft.tags:
    new Set([...q('#capability-choices').querySelectorAll('input:checked')].map(input=>input.value));
  return {address:JSON.parse(selected),expected_updated_at:S.roomAgentUpdatedAt,
    capabilities:[...new Set([...current,...S.roomAgentTags.filter(tag=>!visible.has(tag))])]};
}
async function loadRoomMembers(room){
  const epoch=S.epoch,cursor=room.members_older_cursor;
  if(!cursor)return;
  try{
    const page=await api(url("rooms/"+encodeURIComponent(room.name)+"/members?after="+
      encodeURIComponent(cursor)));
    if(epoch!==S.epoch||cursor!==room.members_older_cursor)return;
    room.members.push(...page.members);room.members_older_cursor=page.members_older_cursor;
    room.member_count=page.member_count;renderRooms();renderAgents();
  }catch(e){notice(e.message,true);}
}
function renderAgents(){
  const all=new Map();
  for(const a of S.agents)all.set(JSON.stringify(a.address),a);
  const render=a=>{
    const presence=a.online?"online":a.last_activity_at&&Date.now()/1000-a.last_activity_at<900?
      "recent":"offline";
    const client=a.address[2];
    const harness=client==="codex"?"Codex":client==="claude"?"Claude":client;
    const n=item(label(a.address),"Last seen: "+relative(a.last_activity_at)+
      " · "+when(a.last_activity_at),client==="claude"||client==="codex"?"":"◇",
      presence,"harness-icon harness-"+client);
    n.children[0].children[0].append(make("span","harness-label",harness));
    const icon=n.children[0].children[0].children[0];
    if(client==="claude"||client==="codex"){
      const logo=make("img","harness-logo");
      logo.src=client==="claude"?"/assets/claude.svg":"/assets/codex.svg";
      logo.alt=harness+" harness logo";icon.append(logo);
    }else icon.setAttribute("aria-label",harness+" harness symbol");
    for(const session of a.sessions||[]){
      const card=make("div","agent-session");
      card.append(make("small","",(session.online?"Online session: ":"Session: ")+
        a.address.join("-")+"-"+session.session_id+" · Model: "+
        (session.model||"Model unknown")));
      if(session.work_status){
        const stale=!session.online||!session.work_updated_at||
          Date.now()/1000-session.work_updated_at>30*60;
        card.append(make("p","agent-work",session.work_status+" · "+
          (stale?"stale update, last reported ":"updated ")+relative(session.work_updated_at)+
          " ("+when(session.work_updated_at)+")"));
      }else card.append(make("small","","No work status reported"));
      n.append(card);
    }
    const tags=make("div","metadata");
    for(const r of S.rooms.filter(r=>r.members.some(m=>JSON.stringify(m)===JSON.stringify(a.address))))
      tags.append(make("span","chip","# "+r.name));
    for(const cap of S.caps.find(c=>JSON.stringify(c.address)===JSON.stringify(a.address))?.capabilities||[])
      tags.append(make("span","chip",cap));
    n.append(tags);
    if(!["human","webui"].includes(a.address[2])){
      const dm=make("button","text-button","DM");dm.type="button";
      dm.setAttribute("aria-label","DM "+label(a.address));
      dm.addEventListener("click",()=>openAgentDm(a.address));n.append(dm);
      const settings=make("button","text-button","Settings");settings.type="button";
      settings.setAttribute("aria-label","Settings for "+label(a.address));
      settings.addEventListener("click",()=>openAgentSettings(a.address));n.append(settings);
    }
    return n;
  };
  const list=[...all.values()].sort((a,b)=>Number(b.online)-Number(a.online));
  $("agent-list").replaceChildren(...(list.length?list.map(render):[empty("Agent presence appears when agents check in.")]));
  $("overview-agents").replaceChildren(...(list.length?list.slice(0,5).map(render):[empty("No agents seen yet.")]));
  $("agent-older").hidden=!S.agentOlder;
  renderAgentPickers();
  renderAgentCapabilityEditor();
}
function renderAgentPickers(){
  const agents=S.agents.filter(a=>!["human","webui"].includes(a.address[2]));
  for(const id of ["dm-recipient","instruction-recipient"]){
    const select=$(id),old=select.value;
    const options=agents.map(a=>{
      const option=make("option","",label(a.address));option.value=JSON.stringify(a.address);
      return option;
    });
    select.replaceChildren(...options);
    if(old&&!options.some(o=>o.value===old)){
      try{const address=JSON.parse(old),option=make("option","",label(address));
        option.value=old;select.append(option);}catch(_){/* Unknown old address. */}
    }
    if([...select.options].some(o=>o.value===old))select.value=old;
  }
  $("instruction-agent-older").hidden=!S.agentOlder||$("instruction-agent-field").hidden;
  $("dm-agent-older").hidden=!S.agentOlder||$("dm-agent-field").hidden;
}
function updateMessageDestination(){
  const room=$("dm-destination").value==="room";
  $("dm-agent-field").hidden=room;
  $("dm-room-field").hidden=!room;
  $("dm-recipient").required=!room;
  $("dm-room").required=room;
  $("dm-agent-older").hidden=room||!S.agentOlder;
}
function renderAssignmentCandidates(){
  const task=S.assignmentTasks.find(t=>t.node_id===$("task-select").value);
  const select=$("assignee-select"),prior=select.value,options=[];
  const placeholder=make("option","","Choose an eligible room member");
  placeholder.value="";placeholder.disabled=true;options.push(placeholder);
  let eligible=0;
  for(const candidate of S.candidates){
    const key=JSON.stringify(candidate.address);
    const choice=make("option","",label(candidate.address)+(candidate.pinned?
      " — selected; eligibility rechecked on submit":candidate.missing.length?
      " — missing: "+candidate.missing.join(", "):" — eligible"));
    choice.value=key;choice.disabled=candidate.missing.length>0;
    options.push(choice);if(!candidate.missing.length)eligible++;
  }
  select.replaceChildren(...options);
  if(prior&&options.some(option=>option.value===prior))select.value=prior;
  else select.value="";
  const selected=options.find(option=>option.value===select.value);
  $("task-assign-form").querySelector('[type="submit"]').disabled=
    !selected||selected.disabled;
  $("assign-eligibility").textContent=task?
    (selected?.disabled&&select.value?"Selected member now lacks required tags; choose another. ":"")+
    eligible+" eligible of "+S.candidates.length+" shown ("+S.candidateMemberCount+" room members) · " +
      "Capabilities are assigned by project users; the server checks again when assigning.":
    "Choose an available task to see eligible room members.";
  $("candidate-older").hidden=!S.candidateOlder;
}
async function loadCandidates({older=false}={}){
  const taskId=$("task-select").value,epoch=S.epoch,request=++S.candidateRequest;
  const cursor=S.candidateOlder;
  if(!taskId){S.candidates=[];S.candidateTask=null;S.candidateOlder=null;
    S.candidateMemberCount=0;renderAssignmentCandidates();return;}
  if(older&&!cursor)return;
  if(!older&&S.candidateTask!==taskId){S.candidates=[];S.candidateOlder=null;
    $("assignee-select").replaceChildren();
    S.candidateTask=taskId;S.candidateMemberCount=0;renderAssignmentCandidates();}
  try{
    const result=await api(url("tasks/"+encodeURIComponent(taskId)+"/candidates"+
      (older?"?after="+encodeURIComponent(cursor):"")));
    if(epoch!==S.epoch||request!==S.candidateRequest||taskId!==$("task-select").value)return;
    if(older)S.candidates.push(...result.candidates);
    else {
      const selected=$("assignee-select").value;
      const pinned=S.candidateTask===taskId&&S.candidates.find(
        c=>JSON.stringify(c.address)===selected);
      S.candidates=result.candidates;
      if(pinned&&!S.candidates.some(c=>JSON.stringify(c.address)===selected))
        S.candidates.push({...pinned,pinned:true});
    }
    S.candidateTask=taskId;S.candidateMemberCount=result.member_count;
    S.candidateOlder=result.older_cursor;
    renderAssignmentCandidates();
  }catch(e){if(epoch===S.epoch)notice(e.message,true);}
}
function renderTasks(){
  const totals=$("task-counts"),counts=S.taskCounts||{};
  totals.replaceChildren(...[["available","Unclaimed"],["assigned_waiting","Assigned"],
    ["in_progress","Claimed"],["complete","Completed"]].map(([state,title])=>
    make("span","chip "+state,(counts[state]||0)+" "+title)));
  const render=t=>{
    const r=S.rooms.find(r=>r.room_id===t.room_id);
    const n=item(t.title||t.node_id,null,"☷",t.state.replaceAll("_"," "));
    n.append(make("small","","Room: "+(r?.name||"—")+" · Assignee: "+label(t.assignee)));
    const detail=expandedDetail(n,t.summary||"Graph task",t.node_id,S.expandedTasks,
                                "task details for "+(t.title||t.node_id));
    if(t.claim){
      const timing=make("small","","Heartbeat: "+when(t.claim.last_beat_at)+
        " · Claim expires: "+when(t.claim.expires_at)+
        (t.claim.progress_overdue?" · Room progress overdue":""));
      detail.append(timing);
    }
    const tags=make("div","metadata");
    tags.append(make("span","muted tiny","Required capabilities: "));
    for(const cap of t.required_capabilities||[])tags.append(make("span","chip",cap));
    if(!t.required_capabilities?.length)tags.append(make("span","muted tiny","none"));
    n.append(tags);
    if(t.assignee&&t.state!=="complete"){
      const clear=make("button","text-button","Clear assignment");clear.type="button";
      clear.addEventListener("click",()=>{
        if(window.confirm("Clear assignment of "+label(t.assignee)+" on "+
          (t.title||t.node_id)+(t.state==="in_progress"?" and revoke its active claim?":"?")))
          mutate("tasks/"+encodeURIComponent(t.node_id)+"/clear",
            {expected_revision:t.revision,confirm_displace:true},"Assignment cleared.");
      });n.append(clear);
    }
    return n;
  };
  $("task-list").replaceChildren(...(S.tasks.length?S.tasks.map(render):[empty("No graph tasks yet. Create one in a room.")]));
  const sel=$("task-select"),prior=sel.value,options=S.assignmentTasks.filter(t=>t.state!=="complete").map(t=>{
    const n=make("option","",t.title||t.node_id);n.value=t.node_id;return n;
  });
  sel.replaceChildren(...options);if(S.assignmentTasks.some(t=>t.node_id===prior))sel.value=prior;
  $("task-older").hidden=!S.taskOlder;
  $("task-select-older").hidden=!S.assignmentOlder;
  loadCandidates();
}
function renderInstructions(){
  const render=i=>{
    const n=item(i.body,"To: "+label(i.recipient)+" · From "+i.author,"✉",i.stalled?"stalled":i.state);
    n.append(make("small","",when(i.created_at)));
    if(i.result)n.append(make("p","body",i.result));
    if(i.state==="queued"){
      const b=make("button","text-button","Cancel instruction");b.type="button";
      b.addEventListener("click",()=>{
        if(window.confirm("Cancel instruction to "+label(i.recipient)+": "+i.body.slice(0,80)+"?"))
          mutate("instructions/"+encodeURIComponent(i.id)+"/cancel",{},"Instruction cancelled.");
      });
      n.append(b);
    }
    if(i.state==="failed"||i.stalled){
      const retry=make("button","text-button","Retry explicitly");retry.type="button";
      retry.addEventListener("click",()=>{
        const room=S.rooms.find(r=>r.room_id===i.room_id)?.name||null;
        mutate("instructions",{room,to_manager:i.to_manager,address:i.recipient,body:i.body,
          retry_of:i.id,idempotency_key:crypto.randomUUID()},"Retry queued as a new attempt.");
      });n.append(retry);
    }
    return n;
  };
  $("instruction-list").replaceChildren(...(S.instructions.length?S.instructions.map(render):[empty("No instructions queued.")]));
  $("instruction-older").hidden=!S.instructionOlder;
}
async function messages({older=false}={}){
  if(!S.project||S.view!=="messages")return;
  const epoch=S.epoch,conversation=older?S.conversationEpoch:++S.conversationEpoch;
  const channel=$("message-channel").value,room=$("message-room").value;
  if(!older){S.olderCursor=null;$("message-older").hidden=true;}
  if(channel==="room"&&!room){$("message-list").replaceChildren(empty("Create a room to start a conversation."));return;}
  try{
    const data=await api(url("messages?channel="+encodeURIComponent(channel)+
      (room?"&room="+encodeURIComponent(room):"")+"&limit=100"+
      (older&&S.olderCursor?"&before_seq="+S.olderCursor:"")));
    if(epoch!==S.epoch||conversation!==S.conversationEpoch||S.view!=="messages"||
       channel!==$("message-channel").value||room!==$("message-room").value)return;
    const newest=data.messages.at(-1)?.seq||null;
    const cards=[...data.messages].reverse().map(m=>{
      const from=m.sender_origin==="human_ui"?m.sender[0]+" · human":label(m.sender);
      const n=item(from,null,m.channel==="dm"?"✉":"◌",
        m.kind==="progress"?"progress":"");
      expandedDetail(n,m.body,m.id,S.expandedMessages,"message from "+from,m.summary);
      n.append(make("small","",when(m.created_at)+(m.recipient?" · To "+label(m.recipient):"")+
        (m.sender_origin==="human_ui"?" · Human":"")));return n;
    });
    if(older)$("message-list").append(...cards);
    else $("message-list").replaceChildren(...(cards.length?cards:[empty("No recent messages here.")]));
    S.olderCursor=data.older_cursor;
    $("message-older").hidden=!S.olderCursor;
    if(data.history_gap)notice("Earlier messages expired after 24 hours.");
    if(!older){
      const unread=data.unread_count;
      S.hasHiddenUnseen=unread>data.messages.filter(m=>m.seq>data.last_read_seq).length;
      S.latestDisplayedSeq=newest;
      $("message-unread").hidden=!unread;
      $("message-unread").textContent=unread+" new";
    }
    if(S.latestDisplayedSeq&&(!S.hasHiddenUnseen||!S.olderCursor)){
      await api(url("messages/mark-read"),{method:"POST",body:JSON.stringify({
        channel,room:channel==="room"?room:null,up_to_seq:S.latestDisplayedSeq
      })});
      S.hasHiddenUnseen=false;$("message-unread").hidden=true;
    }
  }catch(e){if(epoch===S.epoch&&conversation===S.conversationEpoch)notice(e.message,true);}
}
async function refresh(){
  if(!S.project)return;const epoch=S.epoch,status=$("task-status-filter").value;
  try{
    const [r,a,t,i,summary,unfiltered,catalog,pending]=await Promise.all([
      ...["rooms","agents","tasks?status="+encodeURIComponent(status),"instructions","summary"]
        .map(x=>api(url(x))),
      // brief=1: this second request exists only to keep the assignment dropdown showing every
      // task while the list itself is filtered, and the dropdown reads node_id/title/state —
      // all of which the listing already returns. Without it the server re-ran assignments.view
      // and graph_tasks.read per task, so a filtered view cost ~4 read transactions per task
      // twice over, every 20-second tick.
      status==="all"?Promise.resolve(null):api(url("tasks?brief=1")),
      api(url("capabilities")),api(url("capabilities/pending"))]);
    if(epoch!==S.epoch||status!==$("task-status-filter").value)return;
    // Drop loaded pages at refresh: old cursors can skip inserted members and retain removed ones.
    S.rooms=r.rooms;S.roomOlder=r.older_cursor;
    S.agents=a.agents;S.caps=a.capabilities;S.agentOlder=a.older_cursor;
    const existing=new Map(S.catalog.map(cap=>[cap.name,cap]));
    for(const cap of catalog.capabilities)existing.set(cap.name,cap);
    S.catalog=[...existing.values()].sort((left,right)=>left.name.localeCompare(right.name));
    // The catalog MERGES pages rather than discarding them like rooms and tasks do, so its
    // cursor must not rewind to the end of page one on every tick: that made each 20s refresh
    // cost the user a dead click, because the re-fetched page was already in `existing` and the
    // seen-filter dropped all of it. Once the user has paged, the cursor is theirs to advance —
    // including to null, which means they reached the end and must not be sent back to page two.
    if(!S.catalogPaged)S.catalogOlder=catalog.next_cursor;
    // Replace and drop loaded pages, exactly as the rooms and tasks lists do. The catalog above
    // merges because capability DEFINITIONS are append-only, so a kept page cannot go stale;
    // pending assignments are the opposite — they disappear the moment anyone approves them.
    // Merging kept advertising an agent as pending, with its old tags, after another console
    // session (or this one's own config form) had already confirmed it.
    S.pendingAgents=pending.agents;
    S.pendingOlder=pending.next_cursor;S.pendingPaged=false;
    const selectedTask=$("task-select").value;
    const pinnedTask=S.assignmentTasks.find(task=>task.node_id===selectedTask);
    S.tasks=t.tasks;
    // Copy, never alias: with no status filter `unfiltered` is null and both fields pointed at
    // the SAME array, so pushing the pinned task below — or "Load more assignable tasks", which
    // pushes a whole page — silently appended into the rendered Tasks list the user is reading.
    S.assignmentTasks=[...(unfiltered||t).tasks];
    S.assignmentOlder=(unfiltered||t).older_cursor;
    if(pinnedTask&&!S.assignmentTasks.some(task=>task.node_id===selectedTask))
      S.assignmentTasks.push(pinnedTask);
    S.instructions=i.instructions;S.taskCounts=t.counts;S.taskOlder=t.older_cursor;
    S.instructionOlder=i.older_cursor;
    $("breadcrumb-project").textContent=S.project.toUpperCase();
    $("refreshed").textContent="Updated "+new Date().toLocaleTimeString();
    renderRooms();renderCatalog();renderPendingAssignments();
    renderAgents();renderTasks();renderInstructions();
    if(S.view==="agents"&&S.roomAgentKey)await loadAgentCapabilities();
    $("stat-rooms").textContent=summary.online_agents;
    $("stat-agents").textContent=summary.waiting_assignments;
    $("stat-tasks").textContent=summary.overdue_updates;
    $("stat-queue").textContent=summary.queued_instructions+" / "+summary.stalled_instructions;
    if(S.view==="messages")await messages();
  }catch(e){if(epoch===S.epoch)notice(e.message,true);}
}
async function loadProjects(){
  const epoch=S.epoch;
  const p=await api("/api/projects");
  if(epoch!==S.epoch)return;
  resetProjectState();
  $("project-switcher").replaceChildren();
  selectOptions($("project-switcher"),p.projects);
  S.project=$("project-switcher").value||null;S.epoch++;
  if(S.project)await refresh();else notice("No accessible projects. Ask a project owner to share one.",true);
}
async function mutate(tail,data,success,onSuccess){
  const epoch=S.epoch;
  try{const result=await api(url(tail),{method:"POST",body:JSON.stringify(data)});
    if(epoch!==S.epoch)return;
    if(onSuccess)onSuccess(result);
    const warning=result.assignment_warning||
      (result.assignment_notification!==undefined&&result.assignment_notification!=="stored"?
        "Agent notification "+result.assignment_notification:null)||
      (result.room_notification!==undefined&&result.room_notification!=="stored"?
        "Room notification "+result.room_notification:null)||
      (result.notification_warnings?.length?result.notification_warnings.join("; "):null);
    notice(warning?success+" Warning: "+warning:success,!!warning);await refresh();}
  catch(e){if(epoch===S.epoch)notice(e.message,true);}
}
function form(id,handler){
  $(id).addEventListener("submit",async e=>{
    e.preventDefault();const f=e.currentTarget,b=f.querySelector('[type="submit"]');b.disabled=true;
    try{await handler(f,new FormData(f));}
    finally{b.disabled=false;if(id==="task-assign-form")renderAssignmentCandidates();}
  });
}
function init(){
  for(const group of document.querySelectorAll("[data-address-fields]"))
    for(const part of ["user","device","client"]){
      const wrapper=make("label","",part[0].toUpperCase()+part.slice(1)),input=make("input");
      input.name="address_"+part;input.required=true;
      input.placeholder={user:"ana",device:"laptop",client:"claude"}[part];wrapper.append(input);group.append(wrapper);
    }
  const managerOnly=q('#instruction-form [name="to_manager"]');
  const recipientFields=$("instruction-agent-field");
  managerOnly.addEventListener("change",()=>{
    recipientFields.hidden=managerOnly.checked;
    $("instruction-recipient").required=!managerOnly.checked;
    $("instruction-agent-older").hidden=managerOnly.checked||!S.agentOlder;
  });
  $("dm-destination").addEventListener("change",updateMessageDestination);
  form("login-form",async(f,d)=>{
    const epoch=++S.epoch; // Invalidate any pre-login session-resume request immediately.
    try{
      const result=await api("/api/login",{method:"POST",body:JSON.stringify({token:d.get("token")})});
      if(epoch!==S.epoch)return;
      $("login-token").value="";S.csrf=result.csrf_token;
      showVersion(result);
      $("sidebar-user").textContent=result.user+" / "+result.device;
      $("login-error").textContent="";$("login-screen").hidden=true;$("app-shell").hidden=false;
      await loadProjects();
    }catch(e){if(epoch===S.epoch)$("login-error").textContent=e.message;}
  });
  $("logout").addEventListener("click",async()=>{
    const epoch=S.epoch;
    try{await api("/api/logout",{method:"POST",body:"{}"});}catch(_){/* Token already revoked. */}
    if(epoch===S.epoch)showLogin();
  });
  $("project-switcher").addEventListener("change",e=>{
    const next=e.target.value;resetProjectState();S.project=next;S.epoch++;
    notice("");refresh();
  });
  $("nav").addEventListener("click",e=>{const b=e.target.closest("[data-view]");if(b)switchView(b.dataset.view);});
  document.addEventListener("click",e=>{const b=e.target.closest("[data-go]");if(b)switchView(b.dataset.go);});
  $("refresh").addEventListener("click",refresh);
  async function loadMoreAgents(){
    if(!S.agentOlder)return;
    const epoch=S.epoch,cursor=S.agentOlder;
    try{
      const page=await api(url("agents?after="+encodeURIComponent(cursor)));
      if(epoch!==S.epoch||cursor!==S.agentOlder)return;
      S.agents.push(...page.agents);S.caps.push(...page.capabilities);
      S.agentOlder=page.older_cursor;renderAgents();renderAssignmentCandidates();
    }catch(e){notice(e.message,true);}
  }
  for(const id of ["agent-older","dm-agent-older","instruction-agent-older"])
    $(id).addEventListener("click",loadMoreAgents);
  $("room-older").addEventListener("click",async()=>{
    if(!S.roomOlder)return;
    const epoch=S.epoch,cursor=S.roomOlder;
    try{
      const page=await api(url("rooms?after="+encodeURIComponent(cursor)));
      if(epoch!==S.epoch||cursor!==S.roomOlder)return;
      S.rooms.push(...page.rooms);S.roomOlder=page.older_cursor;
      renderRooms();renderAgents();
    }catch(e){notice(e.message,true);}
  });
  $("capability-agent").addEventListener("change",()=>{
    S.roomAgentKey=null;S.roomAgentTags=null;S.roomAgentUpdatedAt=null;
    S.pendingAgentTags=[];S.roomConfigUpdatedAt=null;S.roomConfigDraft=null;
    S.roomChoiceDraft=null;renderAgentCapabilityEditor();
  });
  const updateConfigDraft=()=>S.roomConfigDraft={key:$("capability-agent").value,
    parallel:$("agent-config-parallel").value,auto:$("agent-config-auto-claim").checked};
  $("agent-config-parallel").addEventListener("input",updateConfigDraft);
  $("agent-config-auto-claim").addEventListener("change",updateConfigDraft);
  $("capability-older").addEventListener("click",async()=>{
    if(!S.catalogOlder)return;
    const epoch=S.epoch,cursor=S.catalogOlder;
    try{
      const page=await api(url("capabilities?after="+encodeURIComponent(cursor)));
      if(epoch!==S.epoch||cursor!==S.catalogOlder)return;
      const seen=new Set(S.catalog.map(cap=>cap.name));
      S.catalog.push(...page.capabilities.filter(cap=>!seen.has(cap.name)));
      S.catalogOlder=page.next_cursor;S.catalogPaged=true;
      renderCatalog();renderAgentCapabilityEditor();
    }catch(e){notice(e.message,true);}
  });
  $("pending-agent-older").addEventListener("click",async()=>{
    if(!S.pendingOlder)return;
    const epoch=S.epoch,cursor=S.pendingOlder;
    try{
      const page=await api(url("capabilities/pending?after="+encodeURIComponent(cursor)));
      if(epoch!==S.epoch||cursor!==S.pendingOlder)return;
      const known=new Set(S.pendingAgents.map(agent=>JSON.stringify(agent.address)));
      S.pendingAgents.push(...page.agents.filter(agent=>!known.has(JSON.stringify(agent.address))));
      S.pendingOlder=page.next_cursor;S.pendingPaged=true;renderPendingAssignments();
    }catch(e){notice(e.message,true);}
  });
  $("task-select").addEventListener("change",()=>loadCandidates());
  $("task-status-filter").addEventListener("change",()=>{
    S.tasks=[];S.taskOlder=null;renderTasks();refresh();
  });
  $("assignee-select").addEventListener("change",renderAssignmentCandidates);
  $("candidate-older").addEventListener("click",()=>loadCandidates({older:true}));
  $("task-older").addEventListener("click",async()=>{
    if(!S.taskOlder)return;
    const epoch=S.epoch,cursor=S.taskOlder;
    try{
      const status=$("task-status-filter").value;
      const page=await api(url("tasks?status="+encodeURIComponent(status)+
        "&before_id="+encodeURIComponent(cursor)));
      if(epoch!==S.epoch||cursor!==S.taskOlder||status!==$("task-status-filter").value)return;
      const seen=new Set(S.tasks.map(task=>task.node_id));
      S.tasks.push(...page.tasks.filter(task=>!seen.has(task.node_id)));
      S.taskOlder=page.older_cursor;renderTasks();
    }catch(e){notice(e.message,true);}
  });
  $("task-select-older").addEventListener("click",async()=>{
    if(!S.assignmentOlder)return;
    const epoch=S.epoch,cursor=S.assignmentOlder;
    try{
      const page=await api(url("tasks?before_id="+encodeURIComponent(cursor)));
      if(epoch!==S.epoch||cursor!==S.assignmentOlder)return;
      const seen=new Set(S.assignmentTasks.map(task=>task.node_id));
      S.assignmentTasks.push(...page.tasks.filter(task=>!seen.has(task.node_id)));
      S.assignmentOlder=page.older_cursor;renderTasks();
    }catch(e){notice(e.message,true);}
  });
  $("instruction-older").addEventListener("click",async()=>{
    if(!S.instructionOlder)return;
    const epoch=S.epoch,cursor=S.instructionOlder;
    try{
      const page=await api(url("instructions?before_id="+encodeURIComponent(cursor)));
      if(epoch!==S.epoch||cursor!==S.instructionOlder)return;
      S.instructions.push(...page.instructions);S.instructionOlder=page.older_cursor;
      renderInstructions();
    }catch(e){notice(e.message,true);}
  });
  $("message-channel").addEventListener("change",messages);
  $("message-room").addEventListener("change",messages);
  $("message-older").addEventListener("click",()=>messages({older:true}));
  form("room-create-form",(f,d)=>mutate("rooms",{name:d.get("name").trim(),description:d.get("description").trim()},"Room created."));
  // Snapshot the revision when editing STARTS. refresh() rewrites S.catalog every 20s, so
  // reading it at submit time meant silently adopting whatever another operator had saved in
  // between and overwriting them — the server's compare-and-swap can only protect a revision
  // the user actually saw.
  $("capability-create-form").addEventListener("input",()=>{
    const name=$("capability-create-form").querySelector('[name="name"]').value.trim();
    if(!S.capabilityEditBase||S.capabilityEditBase.name!==name)
      S.capabilityEditBase={name,updated_at:S.catalog.find(cap=>cap.name===name)?.updated_at??null};
  });
  form("capability-create-form",(f,d)=>{
    const name=d.get("name").trim();
    const base=S.capabilityEditBase?.name===name
      ? S.capabilityEditBase.updated_at
      : S.catalog.find(cap=>cap.name===name)?.updated_at??null;
    S.capabilityEditBase=null;
    return mutate("capabilities",{name,description:d.get("description").trim(),
      expected_updated_at:base},"Project capability saved.");
  });
  form("capability-assign-form",(f,d)=>{
    let payload;
    try{payload=selectedAgentCapabilities();}
    catch(e){notice(e.message,true);return;}
    const removed=S.pendingAgentTags.filter(tag=>!payload.capabilities.includes(tag));
    if(removed.length&&!window.confirm("Discard unapproved legacy tags "+removed.join(", ")+
      " for this agent?"))return;
    return mutate("agents/capabilities",payload,
      "Project-wide agent capabilities updated.",result=>{
        S.roomAgentTags=result.capabilities;
        S.pendingAgentTags=result.pending_capabilities||[];
        S.roomAgentUpdatedAt=result.updated_at;S.roomChoiceDraft=null;
        S.pendingAgents=[];S.pendingOlder=null;S.pendingPaged=false;
        renderPendingAssignments();
      });
  });
  form("agent-config-form",(f,d)=>{
    if(!$("capability-agent").value||S.roomAgentTags===null){
      notice("Choose a project agent first.",true);return;
    }
    return mutate("agents/config",{address:JSON.parse($("capability-agent").value),
      max_parallel_tasks:Number($("agent-config-parallel").value),
      auto_claim_enabled:$("agent-config-auto-claim").checked,
      expected_updated_at:S.roomConfigUpdatedAt},"Agent config updated.",result=>{
        S.roomConfigUpdatedAt=result.updated_at;S.roomConfigParallel=result.max_parallel_tasks;
        S.roomConfigAuto=result.auto_claim_enabled;S.roomConfigDraft=null;
      });
  });
  form("member-form",(f,d)=>mutate("rooms/"+encodeURIComponent(d.get("room"))+"/members",{address:address(f)},"Agent added."));
  form("manager-form",async(f,d)=>{
    const epoch=S.epoch,target=address(f);
    try{
      const room=d.get("room"),current=await api(url("rooms/"+encodeURIComponent(room)+"/manager"));
      if(epoch!==S.epoch)return;
      return mutate("rooms/"+encodeURIComponent(room)+"/manager",
        {address:target,expected_revision:current.revision},"Manager promoted.");
    }catch(e){if(epoch===S.epoch)notice(e.message,true);}
  });
  form("task-create-form",(f,d)=>mutate("tasks",{room:d.get("room"),title:d.get("title").trim(),
    summary:d.get("summary").trim(),assign_to_manager:d.get("assign_to_manager")==="on",
    required_capabilities:d.get("capabilities").split(",").map(s=>s.trim()).filter(Boolean)},"Graph task created."));
  form("task-assign-form",(f,d)=>{
    const task=S.assignmentTasks.find(t=>t.node_id===d.get("node_id"));
    const selected=d.get("recipient");
    if(!selected||!task){notice("Choose a task and an eligible room member.",true);return;}
    const target=JSON.parse(selected);
    let confirmDisplace=false;
    if(task.state==="in_progress" && JSON.stringify(task.assignee)!==selected){
      confirmDisplace=window.confirm("Reassign "+(task.title||task.node_id)+" from "+
        label(task.assignee)+" to "+label(target)+" and revoke the active claim?");
      if(!confirmDisplace)return;
    }
    return mutate("tasks/"+encodeURIComponent(d.get("node_id"))+"/assign",
      {address:target,expected_revision:task?.revision??0,
       confirm_displace:confirmDisplace},"Task assigned.");
  });
  form("instruction-form",(f,d)=>mutate("instructions",{room:d.get("room")||null,
    to_manager:d.get("to_manager")==="on",
    address:d.get("to_manager")==="on"?null:JSON.parse(d.get("recipient")),
    body:d.get("body").trim(),idempotency_key:crypto.randomUUID()},"Instruction queued."));
  form("dm-form",(f,d)=>{
    const target=humanMessage(d.get("destination"),d.get("recipient"),d.get("room"),
      d.get("body").trim(),crypto.randomUUID());
    return mutate(target.tail,target.data,"Message sent.");
  });
  setInterval(()=>{if(S.project&&!$("app-shell").hidden)refresh();},20000);
  const resumeEpoch=S.epoch;
  api("/api/session").then(session=>{
    if(resumeEpoch!==S.epoch)return;
    S.csrf=session.csrf_token;$("sidebar-user").textContent=session.user+" / "+session.device;
    showVersion(session);
    $("login-screen").hidden=true;$("app-shell").hidden=false;return loadProjects();
  }).catch(()=>{if(resumeEpoch===S.epoch)showLogin();});
}
init();
