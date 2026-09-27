// Run before the stylesheet is loaded so returning visitors never see the wrong theme flash.
const THEME_KEY = 'hivemind-console-theme';
// Only marked interface copy is themed. Interpolated names, messages and other user content
// stay literal, and switching themes updates existing nodes without rebuilding forms or cards.
window.HivemindTheme = (() => {
  const templates = new WeakMap(), COPY = Symbol('interface-copy');
  const phrases = {
    'Built for people': 'Built for comrades.',
    'working with agents.': 'United in purpose.'
  };
  const terms = {
    'your projects': 'Our missions', 'one clear view': 'One united purpose',
    'online agents': 'Agents on duty', 'room manager': 'General',
    'offer a task': 'Issue an assignment', 'create graph task': 'Issue assignment',
    'queue an instruction': 'Issue a directive', 'queue instruction': 'Issue directive',
    'an instruction': 'a directive', 'graph work items': 'Assignments',
    'graph-backed work': 'Mission assignments', 'graph tasks': 'Assignments',
    'graph task': 'Assignment', 'sign out': 'Leave headquarters',
    'direct messages': 'Direct dispatches', 'direct message': 'Direct dispatch',
    workspace: 'Headquarters', overview: 'Command Briefing',
    projects: 'Missions', project: 'Mission', rooms: 'Collectives', room: 'Collective',
    managers: 'Generals', manager: 'General', capabilities: 'Specialties', capability: 'Specialty',
    tasks: 'Assignments', task: 'Assignment', instructions: 'Directives', instruction: 'Directive',
    messages: 'Dispatches', message: 'Dispatch', dms: 'Direct dispatches', dm: 'Dispatch'
  };
  const statuses = {unclaimed:'Available', available:'Available', assigned:'Issued',
    'assigned waiting':'Issued', claimed:'In progress', 'in progress':'In progress',
    complete:'Accomplished', completed:'Accomplished'};
  const pattern = new RegExp('\\b('+Object.keys(terms).sort((a,b)=>b.length-a.length)
    .join('|')+')\\b', 'gi');
  function matchCase(source, replacement) {
    if(source===source.toUpperCase())return replacement.toUpperCase();
    return source[0]===source[0].toLowerCase()?replacement.toLowerCase():replacement;
  }
  function redText(text) {
    if(Object.hasOwn(phrases,text))return phrases[text];
    const status=statuses[text.trim().toLowerCase()];
    if(status)return text.replace(text.trim(), matchCase(text.trim(), status));
    return text.replace(pattern, match=>matchCase(match, terms[match.toLowerCase()]));
  }
  function copy(strings, ...values) {
    if(strings?.[COPY])return strings;
    return {[COPY]:true, strings:typeof strings==='string'?[strings]:strings, values,
      toString(){return render(this);}};
  }
  function render(value) {
    if(!value?.[COPY])return String(value);
    const red=document.documentElement.dataset.theme==='red';
    return value.strings.map((part,i)=>(red?redText(part):part)+
      (i<value.values.length?render(value.values[i]):'')).join('');
  }
  function write(node, attribute, record) {
    if(record.parts){
      for(const part of record.parts)part.node.nodeValue=render(part.blue);
      return;
    }
    const value=render(document.documentElement.dataset.theme==='red'&&record.red!==undefined?
      record.red:record.blue);
    if(attribute==='text'){
      if(node.textContent!==value)node.textContent=value;
    }else if(node.getAttribute(attribute)!==value)node.setAttribute(attribute,value);
  }
  function set(node, attribute, value, red) {
    const marker='data-theme-'+attribute;
    if(value?.[COPY]||red!==undefined){
      if(!templates.has(node))templates.set(node,new Map());
      const record={blue:value,red};templates.get(node).set(attribute,record);
      node.setAttribute(marker,'');write(node,attribute,record);
    }else{
      templates.get(node)?.delete(attribute);node.removeAttribute(marker);
      write(node,attribute,{blue:value});
    }
  }
  function refresh() {
    for(const node of document.querySelectorAll(
      '[data-theme-text],[data-theme-aria-label],[data-theme-placeholder]')){
      for(const attribute of ['text','aria-label','placeholder']){
        if(!node.hasAttribute('data-theme-'+attribute))continue;
        if(!templates.get(node)?.has(attribute)){
          // Static labels can contain inputs, icons or nested labels: update only their
          // original direct text nodes so event handlers, selections and drafts survive.
          const parts=attribute==='text'?[...(node.childNodes||[])].filter(n=>n.nodeType===3):[];
          if(parts.length){
            if(!templates.has(node))templates.set(node,new Map());
            templates.get(node).set(attribute,{parts:parts.map(n=>({node:n,blue:copy(n.nodeValue)}))});
          }else set(node,attribute,copy(attribute==='text'?node.textContent:node.getAttribute(attribute)));
        }
        write(node,attribute,templates.get(node).get(attribute));
      }
    }
  }
  return {copy, setText:(node,value,red)=>set(node,'text',value,red),
    setAttribute:(node,name,value)=>set(node,name,value), refresh};
})();
function applyTheme(value, save = false) {
  const theme = value === 'red' || value === 'red-alert' ? 'red' : 'blue';
  document.documentElement.dataset.theme = theme;
  for (const picker of document.querySelectorAll('[data-theme-picker]')) picker.value = theme;
  window.HivemindTheme.refresh();
  if (save) {
    try { window.localStorage.setItem(THEME_KEY, theme); } catch (_) { /* Storage is optional. */ }
  }
}
let savedTheme;
try { savedTheme = window.localStorage.getItem(THEME_KEY); } catch (_) { /* Storage is optional. */ }
applyTheme(savedTheme, savedTheme === 'red-alert' || savedTheme === 'current');
document.addEventListener('DOMContentLoaded', () => {
  for (const picker of document.querySelectorAll('[data-theme-picker]'))
    picker.addEventListener('change', event => applyTheme(event.currentTarget.value, true));
  applyTheme(document.documentElement.dataset.theme);
});
