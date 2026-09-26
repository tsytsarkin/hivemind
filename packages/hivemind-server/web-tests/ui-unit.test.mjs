import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const source = readFileSync(fileURLToPath(new URL('../src/hivemind_server/ui_assets/app.js', import.meta.url)), 'utf8');

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
  const document = {createElement: tag => new Element(tag),
    getElementById: id => {if (!ids.has(id)) ids.set(id, new Element()); return ids.get(id);},
    querySelector: () => new Element(), querySelectorAll: () => []};
  const context = vm.createContext({document, fetch, console, Date, URLSearchParams,
    window: {}, crypto: {randomUUID: () => 'once'}});
  vm.runInContext(source.replace(/\binit\(\);\s*$/, ''), context);
  return {ids, context, run: code => vm.runInContext(code, context)};
}

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

test('room capability editor uses project definitions and preserves unloaded agent tags', () => {
  const {ids, run} = app();
  run(`S.rooms=[{name:'reviews',members:[['ana','laptop','claude']]}];
    S.catalog=[{name:'python',description:'Implement Python'},
      {name:'review',description:'Review code'}];
    S.roomAgentKey='["ana","laptop","claude"]';
    S.roomAgentUpdatedAt=123;
    S.roomAgentTags=['review','swift'];
    $('capability-room').value='reviews'; renderRoomCapabilityEditor();`);
  const choices=ids.get('capability-choices').children;
  assert.match(choices[0].textContent, /python.*Implement Python/);
  assert.equal(choices[1].children[0].checked, true);
  choices[0].children[0].checked=true;
  choices[0].children[0].events.change();
  choices[1].children[0].checked=false;
  choices[1].children[0].events.change();
  assert.deepEqual(JSON.parse(JSON.stringify(run('selectedRoomCapabilities()'))),
    {address:['ana','laptop','claude'],expected_updated_at:123,
      capabilities:['swift','python']});
});

test('agent card opens a direct DM with the correct stable recipient preselected', () => {
  const {ids, run} = app();
  run(`S.agents=[{address:['ana','laptop','claude'],online:true,sessions:[]}];
    S.rooms=[]; renderAgents();`);
  const card=ids.get('agent-list').children[0];
  const quick=card.children.find(n=>n.tagName==='button' && n.textContent==='DM');
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
  assert.match(codex.textContent, /Model not reported/);
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
