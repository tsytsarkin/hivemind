const $ = id => document.getElementById(id);
// $ takes an ID, q takes a selector. Three call sites passed a selector to $, which returns null
// for one: the first addEventListener on it threw out of init() before a single form, nav button,
// refresh timer or session resume was bound, so the whole console was inert and login fell back to
// a native form submit. test_ui_assets.py only greps this file for substrings and the Playwright
// spec needs npm and a live server, so nothing in the pytest suite noticed.
const q = selector => document.querySelector(selector);
const S = {csrf:null,project:null,epoch:0,conversationEpoch:0,view:"overview",rooms:[],roomOlder:null,agents:[],caps:[],tasks:[],candidates:[],candidateTask:null,candidateOlder:null,candidateRequest:0,candidateMemberCount:0,instructions:[],olderCursor:null,taskOlder:null,instructionOlder:null,agentOlder:null,latestDisplayedSeq:null,hasHiddenUnseen:false};
const headings = {
  overview:["Overview","YOUR WORKSPACE, AT A GLANCE","The conversations and work moving through your project."],
  rooms:["Rooms","CONVERSATIONS BY TOPIC","Create focused spaces and put together the right team."],
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
const when = t => t?new Date(t*1000).toLocaleString():"Not seen yet";
const relative = t => !t?"not seen":Math.max(0,Math.round((Date.now()/1000-t)/60))+" min ago";
function notice(msg,bad=false) { const n=$("notice"); n.textContent=msg; n.className=bad?"notice error":"notice"; n.hidden=!msg; }
function empty(msg) { return make("p","empty",msg); }
function item(title,detail,symbol,status) {
  const card=make("article","item-card"),top=make("div","item-top"),head=make("div","row-title");
  head.append(make("span","symbol",symbol),make("h3","",title));top.append(head);
  if(status)top.append(make("span","chip "+(/waiting|queued|stalled/.test(status)?"waiting":""),status));
  card.append(top);if(detail)card.append(make("p","",detail));return card;
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
  S.tasks=[];S.taskOlder=null;S.candidates=[];S.candidateTask=null;
  S.candidateOlder=null;S.candidateMemberCount=0;S.candidateRequest++;
  S.instructions=[];S.instructionOlder=null;S.olderCursor=null;
  S.latestDisplayedSeq=null;S.hasHiddenUnseen=false;S.conversationEpoch++;
  for(const node of document.querySelectorAll("[data-room-select]"))node.replaceChildren();
  for(const draft of document.querySelectorAll("#main form"))draft.reset();
  const recipientFields=q('#instruction-form [data-address-fields]');
  recipientFields.hidden=false;
  for(const input of recipientFields.querySelectorAll("input"))input.required=true;
  for(const id of ["task-select","assignee-select"])
    $(id).replaceChildren();
  for(const id of ["room-list","overview-rooms","agent-list","overview-agents",
    "task-list","instruction-list","message-list","message-room-info"])
    $(id).replaceChildren();
  for(const id of ["stat-rooms","stat-agents","stat-tasks","stat-queue"])
    $(id).textContent="—";
  $("breadcrumb-project").textContent="PROJECT";
  $("refreshed").textContent="";notice("");
  $("message-unread").hidden=true;$("message-unread").textContent="";
  $("message-channel").value="room";
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
    const n=item(label(a.address),"Last seen: "+relative(a.last_activity_at)+
      " · "+when(a.last_activity_at),"◎",presence);
    for(const session of a.sessions||[])
      n.append(make("small","",(session.online?"Online session: ":"Session: ")+
        a.address.join("-")+"-"+session.session_id));
    const tags=make("div","metadata");
    for(const r of S.rooms.filter(r=>r.members.some(m=>JSON.stringify(m)===JSON.stringify(a.address))))
      tags.append(make("span","chip","# "+r.name));
    for(const cap of S.caps.find(c=>JSON.stringify(c.address)===JSON.stringify(a.address))?.capabilities||[])
      tags.append(make("span","chip",cap));
    n.append(tags);return n;
  };
  const list=[...all.values()].sort((a,b)=>Number(b.online)-Number(a.online));
  $("agent-list").replaceChildren(...(list.length?list.map(render):[empty("Agent presence appears when agents check in.")]));
  $("overview-agents").replaceChildren(...(list.length?list.slice(0,5).map(render):[empty("No agents seen yet.")]));
  $("agent-older").hidden=!S.agentOlder;
}
function renderAssignmentCandidates(){
  const task=S.tasks.find(t=>t.node_id===$("task-select").value);
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
      "Capabilities are self-advertised; the server checks again when assigning.":
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
  const render=t=>{
    const r=S.rooms.find(r=>r.room_id===t.room_id);
    const n=item(t.title||t.node_id,t.summary||"Graph task","☷",t.state.replaceAll("_"," "));
    n.append(make("small","","Room: "+(r?.name||"—")+" · Assignee: "+label(t.assignee)));
    if(t.claim){
      n.append(make("small","","Heartbeat: "+when(t.claim.last_beat_at)+
        " · Claim expires: "+when(t.claim.expires_at)+
        (t.claim.progress_overdue?" · Room progress overdue":"")));
    }
    const tags=make("div","metadata");
    for(const cap of t.required_capabilities||[])tags.append(make("span","chip",cap));
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
  const sel=$("task-select"),prior=sel.value,options=S.tasks.filter(t=>t.state!=="complete").map(t=>{
    const n=make("option","",t.title||t.node_id);n.value=t.node_id;return n;
  });
  sel.replaceChildren(...options);if(S.tasks.some(t=>t.node_id===prior))sel.value=prior;
  $("task-older").hidden=!S.taskOlder;
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
    const cards=data.messages.map(m=>{
      const n=item(label(m.sender),m.body,m.channel==="dm"?"✉":"◌",m.kind==="progress"?"progress":"");
      n.append(make("small","",when(m.created_at)+(m.recipient?" · To "+label(m.recipient):"")+
        (m.sender_origin==="human_ui"?" · Human":"")));return n;
    });
    if(older)$("message-list").prepend(...cards);
    else $("message-list").replaceChildren(...(cards.length?cards:[empty("No recent messages here.")]));
    S.olderCursor=data.older_cursor;
    $("message-older").hidden=!S.olderCursor;
    if(data.history_gap)notice("Earlier messages expired after 24 hours.");
    if(!older){
      const unread=data.unread_count;
      S.hasHiddenUnseen=unread>data.messages.filter(m=>m.seq>data.last_read_seq).length;
      S.latestDisplayedSeq=data.messages.at(-1)?.seq||null;
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
  if(!S.project)return;const epoch=S.epoch;
  try{
    const [r,a,t,i,summary]=await Promise.all(
      ["rooms","agents","tasks","instructions","summary"].map(x=>api(url(x))));
    if(epoch!==S.epoch)return;
    // Drop loaded pages at refresh: old cursors can skip inserted members and retain removed ones.
    S.rooms=r.rooms;S.roomOlder=r.older_cursor;
    S.agents=a.agents;S.caps=a.capabilities;S.agentOlder=a.older_cursor;
    const selectedTask=$("task-select").value;
    const pinnedTask=S.tasks.find(task=>task.node_id===selectedTask);
    S.tasks=t.tasks;
    if(pinnedTask&&!S.tasks.some(task=>task.node_id===selectedTask))S.tasks.push(pinnedTask);
    S.instructions=i.instructions;S.taskOlder=t.older_cursor;S.instructionOlder=i.older_cursor;
    $("breadcrumb-project").textContent=S.project.toUpperCase();
    $("refreshed").textContent="Updated "+new Date().toLocaleTimeString();
    renderRooms();renderAgents();renderTasks();renderInstructions();
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
async function mutate(tail,data,success){
  const epoch=S.epoch;
  try{await api(url(tail),{method:"POST",body:JSON.stringify(data)});
    if(epoch!==S.epoch)return;
    notice(success);await refresh();}
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
  const recipientFields=q('#instruction-form [data-address-fields]');
  managerOnly.addEventListener("change",()=>{
    recipientFields.hidden=managerOnly.checked;
    for(const input of recipientFields.querySelectorAll("input"))input.required=!managerOnly.checked;
  });
  form("login-form",async(f,d)=>{
    const epoch=++S.epoch; // Invalidate any pre-login session-resume request immediately.
    try{
      const result=await api("/api/login",{method:"POST",body:JSON.stringify({token:d.get("token")})});
      if(epoch!==S.epoch)return;
      $("login-token").value="";S.csrf=result.csrf_token;
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
  $("agent-older").addEventListener("click",async()=>{
    if(!S.agentOlder)return;
    const epoch=S.epoch,cursor=S.agentOlder;
    try{
      const page=await api(url("agents?after="+encodeURIComponent(cursor)));
      if(epoch!==S.epoch||cursor!==S.agentOlder)return;
      S.agents.push(...page.agents);S.caps.push(...page.capabilities);
      S.agentOlder=page.older_cursor;renderAgents();renderAssignmentCandidates();
    }catch(e){notice(e.message,true);}
  });
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
  $("task-select").addEventListener("change",()=>loadCandidates());
  $("assignee-select").addEventListener("change",renderAssignmentCandidates);
  $("candidate-older").addEventListener("click",()=>loadCandidates({older:true}));
  $("task-older").addEventListener("click",async()=>{
    if(!S.taskOlder)return;
    const epoch=S.epoch,cursor=S.taskOlder;
    try{
      const page=await api(url("tasks?before_id="+encodeURIComponent(cursor)));
      if(epoch!==S.epoch||cursor!==S.taskOlder)return;
      const seen=new Set(S.tasks.map(task=>task.node_id));
      S.tasks.push(...page.tasks.filter(task=>!seen.has(task.node_id)));
      S.taskOlder=page.older_cursor;renderTasks();
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
    summary:d.get("summary").trim(),required_capabilities:d.get("capabilities").split(",").map(s=>s.trim()).filter(Boolean)},"Graph task created."));
  form("task-assign-form",(f,d)=>{
    const task=S.tasks.find(t=>t.node_id===d.get("node_id"));
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
    to_manager:d.get("to_manager")==="on",address:address(f),body:d.get("body").trim(),
    idempotency_key:crypto.randomUUID()},"Instruction queued."));
  form("dm-form",(f,d)=>mutate("dm",{address:address(f),body:d.get("body").trim(),
    idempotency_key:crypto.randomUUID()},"Message sent."));
  setInterval(()=>{if(S.project&&!$("app-shell").hidden)refresh();},20000);
  const resumeEpoch=S.epoch;
  api("/api/session").then(session=>{
    if(resumeEpoch!==S.epoch)return;
    S.csrf=session.csrf_token;$("sidebar-user").textContent=session.user+" / "+session.device;
    $("login-screen").hidden=true;$("app-shell").hidden=false;return loadProjects();
  }).catch(()=>{if(resumeEpoch===S.epoch)showLogin();});
}
init();
