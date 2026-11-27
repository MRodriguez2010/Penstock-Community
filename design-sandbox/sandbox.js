const jobs = {
  brief: {title:"Weekly executive brief", kicker:"Operations · Weekly review", intro:"Two decisions need an owner before Friday.", sections:[["Decide","Approve the supplier renewal before Friday to preserve current pricing.","Purchasing update"],["Follow up","The onboarding guide is four days overdue. Operations owns the next draft.","Project tracker"]], steps:["Group this week’s updates","Highlight decisions and overdue items","Preview the brief","Save an approved local copy"], action:"Approve brief", consequence:"Saves the example brief in this sandbox."},
  intake:{title:"Review an intake request",kicker:"Operations · Intake",intro:"The service desk needs a decision on an urgent access request.",sections:[["Request","A new teammate needs access to the shared training folder.","Sample intake form"],["Proposed row","Training folder access · Urgent · Operations","Reviewed intake table"]],steps:["Receive request","Classify urgency","Ask for review","Record the approved request"],action:"Approve request",consequence:"Adds one row to the example intake table."},
  failure:{title:"Investigate a failed run",kicker:"Operations · Run evidence",intro:"Two weekly briefs stopped at the same input step.",sections:[["Where it stopped","The source supplied “Pending” for a field expecting a number.","Input step · Version 2"],["Suggested change","Accept the field as text and test both saved examples before changing the workflow.","Two recorded runs"]],steps:["Inspect the failed input","Try the proposed field change","Compare two sample results","Review before publication"],action:"Test the suggestion",consequence:"Replays two fixed examples. The production workflow stays on its current version."}
};
const get=id=>document.getElementById(id);
const decisions=new Map();let active='brief';
function render(){
 const job=jobs[active],saved=decisions.get(active);
 get('heading').textContent=job.title;get('kicker').textContent=job.kicker;get('intro').textContent=job.intro;
 const content=get('content');content.replaceChildren();
 for(const [name,text,source] of job.sections){const s=document.createElement('section');for(const [tag,value] of [['h3',name],['p',text],['small',source]]){const n=document.createElement(tag);n.textContent=value;s.append(n)}content.append(s)}
 get('steps').replaceChildren(...job.steps.map(s=>{const item=document.createElement('li');item.textContent=s;return item}));
 get('decision').textContent=saved?'Review complete':active==='failure'?'Test a repair':'Ready for review';get('consequence').textContent=job.consequence;
 get('approve').textContent=job.action;get('approve').disabled=!!saved;get('revise').disabled=!!saved;get('revision').hidden=true;get('reason').value='';
 get('receipt').textContent=saved||'';document.querySelectorAll('[data-job]').forEach(b=>b.setAttribute('aria-current',String(b.dataset.job===active)));
}
document.querySelectorAll('[data-job]').forEach(b=>b.onclick=()=>{active=b.dataset.job;render()});
get('approve').onclick=()=>{decisions.set(active,active==='failure'?'Both sample inputs passed in rehearsal. Publication requires a separate decision.':'Approved · Sample result saved locally for this session.');render()};
get('revise').onclick=()=>{get('revision').hidden=false;get('reason').focus()};
get('revision').onsubmit=e=>{e.preventDefault();if(get('reason').value.trim()){decisions.set(active,'Changes requested · '+get('reason').value.trim());render()}};
get('theme').onclick=()=>get('theme').setAttribute('aria-pressed',String(document.body.classList.toggle('dark')));
render();
