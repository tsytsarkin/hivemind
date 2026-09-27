import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const source = readFileSync(fileURLToPath(new URL('../src/hivemind_server/ui_assets/app.js', import.meta.url)), 'utf8');
const shell = readFileSync(fileURLToPath(new URL('../src/hivemind_server/ui_assets/index.html', import.meta.url)), 'utf8');
const KNOWN_IDS = new Set([...shell.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]));

function app(fetch) {
  const ids = new Map();
  class Element {
    constructor(tag = 'div') { this.tagName = tag; this.children = []; this._text = ''; this.value = ''; this.hidden = false; this.events = {}; }
    get options() { return this.children; }
    set textContent(value) { this._text = String(value); this.children = []; }
    get textContent() { return this._text + this.children.map(c => c.textContent ?? String(c)).join(''); }
    append(...children) { this.children.push(...children); }
    prepend(...children) { this.children.unshift(...children); }
    replaceChildren(...children) { this.children = children; this._text = ''; }
    setAttribute(name, value) { this[name] = value; }
    addEventListener(name, fn) { this.events[name] = fn; }
    hasAttribute() { return false; }
    querySelector() { return new Element('button'); }
  }
  // Only ids the real shell defines resolve; everything else is null, exactly as a browser
  // behaves. Auto-creating an element for ANY id meant `$` could never return null here, so this
  // harness could not see the bug it exists to prevent — a CSS selector passed to
  // getElementById, which threw out of init() and left the whole console inert. Verified by
  // reintroducing that bug: with auto-creation all ten tests still passed.
  const document = {createElement: tag => new Element(tag),
    getElementById: id => {
      if (!KNOWN_IDS.has(id)) return null;
      if (!ids.has(id)) ids.set(id, new Element());
      return ids.get(id);
    },
    querySelector: () => new Element(), querySelectorAll: () => [],
    addEventListener: () => {}};
  const context = vm.createContext({document, fetch, console, Date, URLSearchParams,
    setInterval: () => 0, clearInterval: () => {},
    window: {}, crypto: {randomUUID: () => 'once'}});
  vm.runInContext(source.replace(/\binit\(\);\s*$/, ''), context);
  return {ids, context, run: code => vm.runInContext(code, context)};
}

test('console version uses authenticated server metadata and has an unknown fallback', () => {
  const {ids, run} = app();
  run("showVersion({version:'1.5.3'})");
  assert.equal(ids.get('server-version').textContent, 'v1.5.3');
  run('showVersion({version:null})');
  assert.equal(ids.get('server-version').textContent, 'Version unavailable');
});

test('task cards hide full summary while preserving visible status and required tags', () => {
  const {ids, run} = app();
  run(`S.rooms=[{room_id:'r1',name:'reviews'}];
    S.tasks=[{node_id:'task1',title:'Audit',summary:'a long private detail meant to be expanded',
      state:'available',required_capabilities:['review'],room_id:'r1',assignee:null}];
    S.taskCounts={available:1,assigned_waiting:0,in_progress:0,complete:0};
    renderTasks();`);
  const card = ids.get('task-list').children[0];
  const detail = card.children.find(n => n.tagName === 'details');
  assert.ok(detail, 'task details must be expandable');
  assert.match(card.textContent, /Required capabilities.*review/);
  assert.match(ids.get('task-counts').textContent, /1.*[Uu]nclaimed/);
  assert.equal(detail.open, false);
  detail.open = true;
  detail.events.toggle?.();
  run('renderTasks()');
  assert.equal(ids.get('task-list').children[0].children.find(n => n.tagName === 'details').open, true);
});

test('DMs are newest-first and collapsed, while mark-read uses the newest sequence', async () => {
  const requests = [];
  const msgs = [{id:'a',seq:1,channel:'dm',sender:['nik','mac','human'],recipient:['ana','pc','codex'],
    body:'older full message requiring expansion',created_at:10},
    {id:'b',seq:2,channel:'dm',sender:['ana','pc','codex'],recipient:['nik','mac','human'],
      body:'newer full message requiring expansion',created_at:11}];
  const {ids, run} = app(async (path, opts) => {
    requests.push({path, opts});
    return {ok:true,json:async()=>path.includes('mark-read')?{ok:true}:{messages:msgs,unread_count:2,
      last_read_seq:0,older_cursor:null}};
  });
  run(`$('message-channel').value='dm'; S.project='default'; S.view='messages'; S.csrf='csrf';`);
  await run('messages()');
  const cards = ids.get('message-list').children;
  assert.match(cards[0].textContent, /newer full/);
  assert.equal(cards[0].children.some(n=>n.tagName==='details'), true);
  assert.equal(JSON.parse(requests.at(-1).opts.body).up_to_seq, 2);
});

test('room progress displays agent-written summary and hides complete context', async () => {
  const {ids, run} = app(async path => ({ok:true,json:async()=>path.includes('mark-read')?
    {ok:true}:{messages:[{id:'p1',seq:1,channel:'room',kind:'progress',
      sender:['nik','mac','codex'],body:'Full context about parser repro and next steps',
      summary:'Reproduced parser issue; next I will verify the fix.',created_at:10}],
      unread_count:0,last_read_seq:1,older_cursor:null}}));
  run(`$('message-channel').value='room'; $('message-room').value='reviews';
    S.project='default'; S.view='messages';`);
  await run('messages()');
  const card = ids.get('message-list').children[0];
  const detail = card.children.find(n=>n.tagName==='details');
  assert.equal(detail?.open, false);
  assert.equal(detail?.children[0].textContent, 'Reproduced parser issue; next I will verify the fix.');
  assert.match(detail?.children[1].textContent, /Full context about parser/);
});

test('online, offline and task states expose distinct badge classes', () => {
  const {run} = app();
  const classes = run(`['online','offline','available','assigned waiting','in progress','complete']
    .map(state => item('Task',null,'◎',state).children[0].children[1].className)`);
  assert.deepEqual([...classes], ['chip online','chip offline','chip available',
    'chip assigned_waiting','chip in_progress','chip complete']);
});

test('filtered task list keeps unfiltered assignment choices and server-wide counts', async () => {
  const requests = [];
  const task = {node_id:'task-b',title:'Assign me',summary:'Task',state:'assigned_waiting',
    required_capabilities:[],room_id:'r1',assignee:null};
  const {run, ids} = app(async path => {
    requests.push(path);
    const body = path.includes('/tasks') ? {tasks:path.includes('status=available')?[]:[task],
      counts:{available:0,assigned_waiting:1,in_progress:0,complete:0},older_cursor:null} :
      path.includes('/rooms') ? {rooms:[],older_cursor:null} :
      path.includes('/agents') ? {agents:[],capabilities:[],older_cursor:null} :
      path.includes('/capabilities/pending') ? {agents:[],next_cursor:null} :
      path.includes('/capabilities') ? {capabilities:[],next_cursor:null} :
      path.includes('/instructions') ? {instructions:[],older_cursor:null} :
      {online_agents:0,waiting_assignments:1,overdue_updates:0,queued_instructions:0,stalled_instructions:0};
    return {ok:true,json:async()=>body};
  });
  run(`S.project='default'; $('task-status-filter').value='available';`);
  await run('refresh()');
  assert.ok(requests.some(path=>path.includes('status=available')));
  assert.equal(ids.get('task-list').children[0].textContent.includes('Assign me'), false);
  assert.match(ids.get('task-select').textContent, /Assign me/);
  assert.match(ids.get('task-counts').textContent, /1 Assigned/);
});

test('overview shows global task counts and recent open work even with Tasks filtered', async () => {
  const sample = [
    {node_id:'t2',title:'Implement fix',state:'in_progress',room_id:'room-1'},
    {node_id:'t1',title:'Review fix',state:'available',room_id:'room-1'},
    {node_id:'t0',title:'Finished fix',state:'complete',room_id:'room-1'},
  ];
  const {ids, run} = app(async path => ({ok:true,json:async()=>path.includes('/tasks')?
    {tasks:path.includes('status=available')?[sample[1]]:
      path.includes('status=open')?sample.filter(t=>t.state!=='complete'):sample,
      counts:{available:1,assigned_waiting:0,in_progress:1,complete:1},older_cursor:null}:
    path.includes('/rooms')?{rooms:[{room_id:'room-1',name:'reviews',members:[]}],older_cursor:null}:
    path.includes('/agents')?{agents:[],capabilities:[],older_cursor:null}:
    path.includes('/capabilities/pending')?{agents:[],next_cursor:null}:
    path.includes('/capabilities')?{capabilities:[],next_cursor:null,revision:0}:
    path.includes('/instructions')?{instructions:[],older_cursor:null}:
    {online_agents:0,waiting_assignments:0,overdue_updates:0,queued_instructions:0,stalled_instructions:0}}));
  run(`S.project='default'; $('task-status-filter').value='available';`);
  await run('refresh()');
  assert.match(ids.get('overview-task-counts').textContent, /1 Unclaimed.*1 Claimed.*1 Completed/);
  assert.match(ids.get('overview-task-list').textContent, /Implement fix.*Review fix/);
  assert.doesNotMatch(ids.get('overview-task-list').textContent, /Finished fix/);
});

test('a catalog revision change drops cached deleted capabilities', async () => {
  const {ids, run} = app(async path => ({ok:true,json:async()=>path.includes('/capabilities/pending')?
    {agents:[],next_cursor:null}:path.includes('/capabilities')?
    {capabilities:[{name:'python',description:'Python',approved:true}],next_cursor:null,revision:2}:
    path.includes('/tasks')?{tasks:[],counts:{available:0,assigned_waiting:0,in_progress:0,complete:0},older_cursor:null}:
    path.includes('/rooms')?{rooms:[],older_cursor:null}:
    path.includes('/agents')?{agents:[],capabilities:[],older_cursor:null}:
    path.includes('/instructions')?{instructions:[],older_cursor:null}:
    {online_agents:0,waiting_assignments:0,overdue_updates:0,queued_instructions:0,stalled_instructions:0}}));
  run(`S.project='default';S.catalog=[{name:'python',approved:true},{name:'review',approved:true}];
    S.catalogRevision=1;S.catalogPaged=true;`);
  await run('refresh()');
  assert.match(ids.get('capability-catalog').textContent, /python/);
  assert.doesNotMatch(ids.get('capability-catalog').textContent, /review/);
  assert.equal(run('S.catalogPaged'), false);
});

test('capability cards offer deletion with the server revision', async () => {
  const requests=[];
  const {ids, run}=app(async (path, options) => {
    requests.push({path,options});
    return {ok:true,json:async()=>path.endsWith('/capabilities/delete')?
      {name:'review',notification_warnings:[]}:
      path.includes('/capabilities/pending')?{agents:[],next_cursor:null}:
      path.includes('/capabilities')?{capabilities:[],next_cursor:null,revision:43}:
      path.includes('/tasks')?{tasks:[],counts:{available:0,assigned_waiting:0,in_progress:0,complete:0},older_cursor:null}:
      path.includes('/rooms')?{rooms:[],older_cursor:null}:
      path.includes('/agents')?{agents:[],capabilities:[],older_cursor:null}:
      path.includes('/instructions')?{instructions:[],older_cursor:null}:
      {online_agents:0,waiting_assignments:0,overdue_updates:0,queued_instructions:0,stalled_instructions:0}};
  });
  run(`S.project='default'; S.catalog=[{name:'review',description:'Review code',
    approved:true,updated_at:42}];S.catalogRevision=42;window.confirm=()=>true;renderCatalog();`);
  const card=ids.get('capability-catalog').children[0];
  const actions=card.children.find(child=>child.className==='catalog-actions');
  const button=actions?.children.find(child=>child.tagName==='button'&&child.textContent==='Delete');
  assert.ok(button);
  await button.events.click();
  const request=requests.find(r=>r.path.endsWith('/capabilities/delete'));
  assert.deepEqual(JSON.parse(request.options.body),{name:'review',expected_updated_at:42});
  assert.doesNotMatch(ids.get('capability-catalog').textContent,/review/);
});

test('agent card groups DM and Settings in a dedicated actions row', () => {
  const {ids,run}=app();
  run(`S.agents=[{address:['nik','mac','codex'],online:true,sessions:[]}];S.rooms=[];renderAgents();`);
  const row=ids.get('agent-list').children[0].children.find(child=>child.className==='agent-actions');
  assert.deepEqual(row?.children.map(child=>child.textContent),['DM','Settings']);
});

test('console browser title and sidebar show Orchestrator Console', () => {
  assert.match(shell, /<title>Hivemind · Orchestrator Console<\/title>/);
  assert.match(shell, /class="brand-caption">ORCHESTRATOR CONSOLE<\/span>/);
});

test('human message destination picks the proper project endpoint without a supplied sender', () => {
  const {run} = app();
  const dm = run(`humanMessage('agent','["ana","laptop","claude"]','',
    'Hello', 'once')`);
  const room = run(`humanMessage('room','', 'reviews', 'Update', 'twice')`);
  assert.equal(dm.tail, 'dm');
  assert.deepEqual(JSON.parse(JSON.stringify(dm.data)),
    {address:['ana','laptop','claude'],body:'Hello',idempotency_key:'once'});
  assert.equal(room.tail, 'messages/room');
  assert.deepEqual(JSON.parse(JSON.stringify(room.data)),
    {room:'reviews',body:'Update',idempotency_key:'twice'});
});

test('Agent settings works for roster agents outside rooms and reviews pending tags', () => {
  const {ids, run} = app();
  assert.match(shell, /data-view="capabilities"/);
  run(`S.rooms=[];S.agents=[{address:['ana','laptop','claude'],online:false,sessions:[]}];
    S.catalog=[{name:'python',description:'Implement Python',approved:true},
      {name:'review',description:'Review code',approved:true},
      {name:'swift',description:'Build Swift apps',approved:true}];
    S.roomAgentKey='["ana","laptop","claude"]';
    S.roomAgentUpdatedAt=123;
    S.roomAgentTags=['review','swift'];
    S.pendingAgentTags=['swift']; renderAgents();`);
  const card=ids.get('agent-list').children[0];
  assert.ok(card.children.find(n=>n.className==='agent-actions')?.children.some(
    n=>n.tagName==='button' && n.textContent==='Settings'));
  assert.equal(ids.get('capability-agent').options[0].value,'["ana","laptop","claude"]');
  const choices=ids.get('capability-choices').children;
  assert.match(choices[0].textContent, /python.*Implement Python/);
  assert.equal(choices[1].children[0].checked, true);
  assert.equal(choices[2].children[0].checked, false, 'pending tags need explicit approval');
  choices[0].children[0].checked=true;
  choices[0].children[0].events.change();
  choices[1].children[0].checked=false;
  choices[1].children[0].events.change();
  assert.deepEqual(JSON.parse(JSON.stringify(run('selectedAgentCapabilities()'))),
    {address:['ana','laptop','claude'],expected_updated_at:123,
      capabilities:['python']});
});

test('agent card opens a direct DM with the correct stable recipient preselected', () => {
  const {ids, run} = app();
  run(`S.agents=[{address:['ana','laptop','claude'],online:true,sessions:[]}];
    S.rooms=[]; renderAgents();`);
  const card=ids.get('agent-list').children[0];
  const quick=card.children.find(n=>n.className==='agent-actions')?.children.find(
    n=>n.tagName==='button' && n.textContent==='DM');
  assert.ok(quick);
  quick.events.click();
  assert.equal(ids.get('dm-destination').value,'agent');
  assert.equal(ids.get('dm-recipient').value,'["ana","laptop","claude"]');
  assert.equal(run('S.view'),'agents');
});

test('agent cards identify harness and show per-session model and timestamped work status', () => {
  const {ids, run} = app();
  run(`S.agents=[{address:['nik','mac','codex'],online:true,last_activity_at:Date.now()/1000,
    sessions:[{session_id:'one',online:true,model:'gpt-6-sol',
      work_status:'Reviewing parser',work_updated_at:Date.now()/1000},
      {session_id:'two',online:false,model:null,work_status:null,work_updated_at:null}]},
    {address:['ana','pc','claude'],online:false,sessions:[]}];
    S.rooms=[]; renderAgents();`);
  const [codex, claude] = ids.get('agent-list').children;
  assert.match(codex.textContent, /Codex.*gpt-6-sol.*Reviewing parser/);
  assert.match(codex.textContent, /Model unknown/);
  assert.match(claude.textContent, /Claude/);
  assert.equal(codex.children[0].children[0].children[0].className.includes('codex'), true);
  assert.equal(claude.children[0].children[0].children[0].className.includes('claude'), true);
  assert.equal(codex.children[0].children[0].children[0].children[0].src,'/assets/codex.svg');
  assert.equal(claude.children[0].children[0].children[0].children[0].src,'/assets/claude.svg');
});

test('stale agent updates are labelled as stale rather than current work', () => {
  const {ids, run} = app();
  run(`S.agents=[{address:['nik','mac','claude'],online:false,sessions:[
    {session_id:'old',model:'claude-opus-4',online:false,
      work_status:'Auditing',work_updated_at:Date.now()/1000-31*60}]}];
    S.rooms=[]; renderAgents();`);
  assert.match(ids.get('agent-list').children[0].textContent, /Auditing.*stale/);
});


test('init() binds the console without throwing', () => {
  // The harness strips the trailing `init();` so each test can drive one render in isolation —
  // which also meant NOTHING here ever executed the binding pass. That is where the console
  // died once: a CSS selector handed to getElementById returned null and the first
  // addEventListener on it threw, aborting init() before any form, nav button, refresh timer or
  // session resume was wired, with every individual render still passing. Run it once, for real.
  const {run} = app(async () => ({projects: []}));
  assert.doesNotThrow(() => run('init()'));
});
