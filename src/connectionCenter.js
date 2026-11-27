import {beginOfficeSignIn} from './officeSignIn.js';
const node = (tag, text, cls) => {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (cls) el.className = cls;
  return el;
};
const states = {'changes-requested':'Changes requested', waiting:'Needs your decision', finished:'Completed', declined:'Declined', interrupted:'Interrupted', failed:'Needs attention'};

export function mountConnections(container, {project,request,admin=false,canTest=true,canEdit=canTest,section='connections',openResult,openDraft,useItems}){
  const root=node('section',undefined,'connection-center');
  container.replaceChildren(root);
  const projectId=typeof project==='function'?project():project;
  const call=(name, data={})=>request(name,{...data,project:projectId});
  const actions=node('nav');actions.setAttribute('aria-label','Workspace services');
  const content=node('div');
  const feedback=node('p');feedback.setAttribute('role','status');
  root.append(actions,feedback,content);
  let generation=0;
  function button(label, action){
    const b=node('button',label);b.type='button';
    b.onclick=async()=>{b.disabled=true;feedback.textContent='';try{await action()}catch(e){feedback.textContent=e.message||'Unable to complete this action.'}finally{b.disabled=false}};
    return b;
  }
  function card(title,description){
    const article=node('article');article.append(node('h3',title),node('p',description));return article;
  }
  function confirmAction(parent, message, label, act){
    parent.querySelector('.connection-confirm')?.remove();
    const area=node('section',undefined,'connection-confirm');
    area.append(node('p',message),button(label,async()=>{await act();area.remove()}),button('Cancel',()=>area.remove()));
    parent.append(area);area.querySelector('button').focus();
  }
  function listItems(records){const list=node('ul');for(const entry of records)list.append(node('li',entry||'Untitled'));return list}
  async function connections(sequence){
    const data=await call('ops-catalog');if(sequence!==generation)return;
    const heading=node('h2','Connections and skills'), ready=node('div'), available=node('div'), more=node('details');
    more.append(node('summary','Services that IT can provision'));
    for(const item of data.items){
      const pane=item.enabled?ready:item.available?available:more;
      const c=card(item.name,item.action);
      const usesSignIn=Object.hasOwn(item,'signedIn');
      const status=item.enabled?'Connected':item.reauthorize?'Needs owner confirmation':
        usesSignIn?(item.signIn?(item.signedIn?'Signed in':'Sign-in required'):'Requires IT setup'):
        item.available?'Ready for setup':'Requires IT setup';
      c.append(node('small',[item.group,status,item.enabled&&item.checked?'Checked '+new Date(item.checked*1000).toLocaleString():null].filter(Boolean).join(' · ')));
      if(admin && item.available && item.id!=='project-files'){
        if(item.enabled)c.append(button('Disconnect',()=>confirmAction(c,'This stops future checks and deliveries through this connection.','Disconnect',async()=>{await call('ops-revoke',{kind:item.id});await show('connections')})));
        else if((!item.delivery||data.canEnableNotifications)&&(!usesSignIn||item.signedIn)){
          const label=item.reauthorize?'Review destination':'Connect';
          const message=item.delivery?'Enable future live-run notifications to the destination provisioned by IT. Messages contain the status and sign-in link.':'Enable this project’s signed-in or registered account. A live read asks for your confirmation.';
          c.append(button(label,()=>confirmAction(c,message,'Enable connection',async()=>{await call('ops-configure',{kind:item.id,confirmDelivery:item.delivery});await show('connections')})));
        }
      }
      if(admin&&item.signIn){
        c.append(button(item.signedIn?'Reconnect account':'Sign in to '+item.group,()=>confirmAction(c,'Open '+item.group+' to authorize this project’s delegated read access.','Continue to sign-in',async()=>{
          await beginOfficeSignIn(request,projectId,item.id);
        })));
      }
      if(canEdit&&item.enabled&&!item.delivery&&openDraft)c.append(button('Use in a workflow',async()=>openDraft(await call('ops-source-draft',{kind:item.id}))));
      if(admin && item.enabled && !item.delivery){
        c.append(button('Preview a read',()=>confirmAction(c,item.action+'. '+(item.group==='local'?'Data stays in this workspace.':'Penstock will contact '+item.group+' using the project’s registered credential. No model receives these results.'),'Read now',async()=>{
          const res=await call('ops-read',{kind:item.id,consent:true});
          c.querySelector('.connection-results')?.remove();const out=node('div',undefined,'connection-results');
          out.append(node('p',res.count+' items returned'),listItems(res.items));
          if(canEdit&&useItems&&res.items.length)out.append(button('Use these items in a draft',()=>useItems(res.items)));
          c.append(out);
        })));
      }
      pane.append(c);
    }
    const skills=card('Installed skills',data.skills.length?'Available for model steps in this workspace.':'No instruction skills are installed yet.');
    if(data.skills.length)skills.append(listItems(data.skills));
    const models=card('Model profiles',data.providers?.length?'Available when drafting or configuring a model step.':'You can run both offline starters before installing a model.');
    if(data.providers?.length)models.append(listItems(data.providers));
    if(data.canInstall){
      function setup(title, fields, endpoint){
        const drawer=node('details'),form=node('form');
        drawer.append(node('summary',title),form);
        for(const [key,label,options] of fields){
          const wrap=node('label',label), input=node(Array.isArray(options)?'select':options==='long'?'textarea':'input');
          input.name=key;input.required=key!=='keyEnv';
          if(Array.isArray(options))for(const [v,text] of options)input.append(new Option(text,v));
          wrap.append(input);form.append(wrap);
        }
        const submit=node('button','Save');submit.type='submit';form.append(submit);
        form.onsubmit=async e=>{e.preventDefault();submit.disabled=true;try{await call(endpoint,Object.fromEntries(new FormData(form)));await show('connections');feedback.textContent='Saved and ready to use.'}catch(err){feedback.textContent=err.message}finally{submit.disabled=false}};
        return drawer;
      }
      models.append(setup('Add a model profile',[['name','Profile name'],['protocol','Protocol',[['ollama','Ollama'],['chat-completions','Chat Completions compatible'],['anthropic-messages','Anthropic Messages compatible']]],['url','Full request URL'],['model','Model ID'],['keyEnv','Credential variable (optional)']],'ops-profile-add'));
      skills.append(setup('Install an instruction skill',[['name','Skill name'],['instructions','Instructions','long']],'ops-skill-add'));
    }
    content.replaceChildren(heading,ready,available,models,skills,more);
  }
  async function inbox(sequence){
    const data=await call('ops-notifications');if(sequence!==generation)return;
    const body=node('div');body.append(node('h2','Inbox'));
    if(!data.items.length)body.append(node('p','Approvals, failures and completed results will appear here.'));
    for(const event of data.items){
      const c=card(event.title,states[event.state]||event.state);
      c.append(node('small','Version '+event.version+' · '+new Date(event.created*1000).toLocaleString()));
      if(openResult)c.append(button('Open result',async()=>{await openResult(event.reference)}));
      if(!event.seen)c.append(button('Mark as read',async()=>{await call('ops-notification-ack',{id:event.id});await show('inbox')}));
      for(const delivery of data.deliveries.filter(d=>d.event===event.id))c.append(node('small',delivery.kind+' · '+delivery.status));
      body.append(c);
    }
    content.replaceChildren(body);
  }
  async function improvements(sequence){
    const res=await call('ops-improvements');if(sequence!==generation)return;
    const body=node('div');body.append(node('h2','Suggested improvements'));
    if(!res.items.length)body.append(node('p','No recurring input problems with a testable change have been found.'));
    for(const item of res.items){
      const c=card(item.name,item.proposal);c.append(node('small',item.evidence.length+' matching failures · Version '+item.version));
      for(const id of item.evidence.slice(0,3))if(openResult)c.append(button('Inspect run '+id.slice(0,8),()=>openResult('run:'+id)));
      if(canTest)c.append(button('Test change on saved examples',async()=>{
        const result=await call('ops-test-change',{workflow:item.workflow,version:item.version,field:item.field});
        c.querySelector('.connection-results')?.remove();
        const out=node('div',undefined,'connection-results');out.append(node('p',result.results.filter(r=>r.after).length+' of '+result.results.length+' inputs accepted after the change.'),node('p',result.summary));
        if(result.passed&&canEdit&&openDraft)out.append(button('Review as a draft',()=>openDraft(result)));
        c.append(out);
      }));body.append(c);
    }
    content.replaceChildren(body);
  }
  async function show(name){
    const sequence=++generation;
    for(const b of actions.children)b.setAttribute('aria-pressed',String(b.dataset.panel===name));
    content.replaceChildren(node('p','Loading…'));
    try{await ({connections,inbox,improvements}[name]||connections)(sequence)}catch(e){if(sequence===generation)content.replaceChildren(node('p',e.message||'Unable to load this view.'))}
  }
  for(const [id,title] of [['connections','Connections'],['inbox','Inbox'],['improvements','Improvements']]){
    const b=button(title,()=>show(id));b.dataset.panel=id;actions.append(b);
  }
  show(section);
  return {open:show};
}
