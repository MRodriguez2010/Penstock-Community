// Run against a disposable managed workspace using Puppeteer supplied by the browser test host.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const puppeteer = require(process.env.PUPPETEER_MODULE || 'puppeteer');
async function main(){
  const data=JSON.parse(fs.readFileSync(0,'utf8'));
  if(!data.disposable || !data.ownerKey || !data.invite)throw Error('Supply a disposable managed test workspace.');
  const browser=await puppeteer.launch({executablePath:process.env.CHROME_BIN,headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
  const shots=[],errors=[],outside=[],audits=[],clips=[];
  const axe=process.env.AXE_SCRIPT && fs.readFileSync(process.env.AXE_SCRIPT,'utf8');
  const beat=()=>new Promise(resolve=>setTimeout(resolve,650));
  async function record(p,name){
    if(!data.clips)return async()=>{};
    const file=name+'.webm';
    const stream=await p.screencast({path:path.join(data.screens,file),format:'webm',fps:12,scale:.75});
    await beat();
    return async()=>{await beat();await stream.stop();assert.ok(fs.statSync(path.join(data.screens,file)).size>1000);clips.push(file)};
  }
  try{
    async function page(width,height){
      const context=await browser.createBrowserContext(),p=await context.newPage();
      await p.setViewport({width,height,deviceScaleFactor:1,isMobile:width<500,hasTouch:width<500});
      p.setDefaultTimeout(8000);
      p.on('pageerror',e=>errors.push(e.message));await p.setRequestInterception(true);
      p.on('request',r=>{if(new URL(r.url()).origin===data.origin||r.url().startsWith('data:')||(data.sandbox && ['index.html','sandbox.css','sandbox.js'].some(file=>r.url()===new URL(file,data.sandbox).href)))return r.continue();outside.push(new URL(r.url()).origin);return r.abort()});
      return p;
    }
    async function press(p,title){
      const selector='button';await p.waitForFunction((sel,text)=>[...document.querySelectorAll(sel)].some(b=>b.textContent.trim()===text&&b.offsetHeight&&!b.disabled),{},selector,title);
      const b=await p.evaluateHandle((sel,text)=>[...document.querySelectorAll(sel)].find(b=>b.textContent.trim()===text&&b.offsetHeight&&!b.disabled),selector,title);
      await b.asElement().click();await b.dispose();
    }
    async function visible(p,text){await p.waitForFunction(t=>document.body.innerText.includes(t),{},text)}
    async function capture(p,name){
      await p.evaluate(()=>document.fonts.ready);
      if(axe){
        await p.evaluate(axe);
        const failures=await p.evaluate(async()=> (await window.axe.run(document,{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21a','wcag21aa','wcag22aa']}})).violations.map(v=>({rule:v.id,impact:v.impact,targets:v.nodes.slice(0,3).map(n=>n.target)})));
        audits.push({screen:name,failures});
      }
      const sizes=await p.evaluate(()=>({viewport:innerWidth,document:document.documentElement.scrollWidth}));
      if(data.screens)await p.screenshot({path:path.join(data.screens,name+'.png')});
      shots.push({name,...sizes});
    }
    const owner=await page(1440,1000);await owner.goto(data.origin);await owner.type('#owner',data.ownerKey);await owner.click('#unlock button');
    await visible(owner,'Open Weekly executive brief');await capture(owner,'01-start-a-job');
    const stopBrief=await record(owner,'weekly-brief-review');
    await press(owner,'Open Weekly executive brief');await press(owner,'Generate brief for review');await visible(owner,'Save this brief to project files?');
    await capture(owner,'02-brief-preview');await press(owner,'Approve');await visible(owner,'Download saved copy');await capture(owner,'03-brief-receipt');await stopBrief();
    await press(owner,'All starters');await press(owner,'Open Reviewed intake');await press(owner,'Submit for review');await visible(owner,'Add this request to the Reviewed intake table?');
    const att=await owner.evaluate(async project=>{
      const headers={'Content-Type':'application/json','Authorization':'Bearer '+sessionStorage.getItem('penstock.key')};
      const response=await fetch('/api/starter-catalog',{method:'POST',headers,body:JSON.stringify({project})});
      return (await response.json()).runs.find(r=>r.kind==='reviewed-intake'&&r.status==='awaiting-review')?.id;
    },data.project);
    assert.ok(att,'Expected an intake awaiting review');
    const exec=await page(390,844);await exec.goto(data.invite);
    const password='form input[autocomplete="new-password"]';await exec.waitForSelector(password,{visible:true});await exec.type(password,'only-for-this-synthetic-test');
    await press(exec,'Accept invitation');await visible(exec,'Open Weekly executive brief');
    await exec.goto(data.origin+'/?'+new URLSearchParams({project:data.project,notice:'starter:'+att}));
    await visible(exec,'Add this request to the Reviewed intake table?');await capture(exec,'04-phone-decision');
    const placement=await exec.evaluate(()=>({
      menu:document.querySelector('#mobileNavigation').getAttribute('aria-expanded'),
      decision:[...document.querySelectorAll('button')].find(b=>b.textContent==='Approve' && b.offsetHeight)?.getBoundingClientRect().bottom
    }));
    assert.equal(placement.menu,'false');assert.ok(placement.decision>0 && placement.decision<=844,'Approval is below the phone viewport: '+placement.decision);
    const stopPhone=await record(exec,'phone-approval');
    await press(exec,'Approve');await visible(exec,'Added a row to Reviewed intake');await capture(exec,'05-phone-receipt');await stopPhone();
    await exec.reload();await visible(exec,'Added a row to Reviewed intake');
    assert.equal(shots.at(-1).document,390,'Phone body width changed');
    await owner.click('#openTeamSetup');await visible(owner,'Team sign-in is active');await capture(owner,'09-team-setup');
    await owner.click('#openConnections');await visible(owner,'Connections and skills');await capture(owner,'06-connections');
    const leader=await page(1440,1000);await leader.goto(data.executive);
    await leader.waitForSelector(password,{visible:true});await leader.type(password,'synthetic-executive-password');
    await press(leader,'Accept invitation');
    await leader.waitForSelector('.guided-other-job textarea',{visible:true});
    assert.ok(await leader.$eval('#openTeamSetup',b=>b.hidden),'Team setup is owner-only');
    await leader.type('.guided-other-job textarea',data.job);await press(leader,'Set up job plan');
    await leader.waitForSelector('#draftConsent',{visible:true});assert.ok(await leader.$eval('#generate',b=>b.disabled));
    await leader.click('#draftConsent');await leader.click('#generate');await visible(leader,'Proposal ready to review');
    await leader.click('#save');await leader.waitForSelector('#test',{visible:true});await leader.click('#test');
    await leader.waitForSelector('#receipt:not([hidden])',{visible:true});await visible(leader,'Prepare a board update');
    await visible(leader,'Approve this board update?');await press(leader,'Approve');await visible(leader,'Finished');
    assert.ok(await leader.$eval('#publish',b=>b.hidden||b.disabled),'Executive release action');
    await capture(leader,'07-executive-approved-plan');
    await press(leader,'Replay receipt offline');await visible(leader,'Matches the receipt');
    await capture(leader,'11-receipt-replay');
    const sandbox=await page(1440,1000);await sandbox.goto(data.sandbox);
    await visible(sandbox,'Two decisions need an owner');await sandbox.click('#approve');await visible(sandbox,'Sample result saved');
    await sandbox.click('[data-job=intake]');await sandbox.click('#revise');await sandbox.type('#reason','Add an owner');
    await sandbox.click('#revision button');await visible(sandbox,'Changes requested · Add an owner');
    await sandbox.click('[data-job=failure]');await sandbox.click('#theme');await sandbox.click('#approve');await visible(sandbox,'Both sample inputs passed');
    await capture(sandbox,'08-design-sandbox');
    await owner.bringToFront();await owner.click('#openStarters');await visible(owner,'Open Weekly executive brief');
    await press(owner,'Open Weekly executive brief');await owner.waitForSelector('#starterDataSource',{visible:true});await owner.select('#starterDataSource','custom');
    await press(owner,'Use a project table');await press(owner,'Load updates');await visible(owner,'Loaded 1 updates');
    await press(owner,'Generate brief for review');await visible(owner,'Confirm renewal date');
    await owner.click('#openStarters');await owner.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));
    await owner.waitForSelector('#inboxCount:not([hidden])');
    await owner.evaluate(()=>document.querySelector('#inboxAlert button:last-child')?.click());
    const stopAlert=await record(owner,'notification-to-decision');
    await owner.evaluate(async project=>{
      const reply=await fetch('/api/starter-run',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+sessionStorage.getItem('penstock.key')},body:JSON.stringify({project,kind:'reviewed-intake',version:1,source:'sample',input:{},request:crypto.randomUUID()})});
      if(!reply.ok)throw Error('Could not generate a disposable pending decision');
      document.dispatchEvent(new Event('visibilitychange'));
    },data.project);
    await owner.waitForSelector('#inboxAlert:not([hidden])');if(data.clips)await beat();
    await owner.click('#inboxAlert button');await visible(owner,'Add this request to the Reviewed intake table?');
    await stopAlert();
    await owner.click('#new');
    await owner.waitForSelector('#proposal label select',{visible:true});
    const selectByLabel=async (label,value)=>{
      const select=await owner.evaluateHandle(title=>[...document.querySelectorAll('#proposal label')].find(l=>l.firstChild?.textContent.trim()===title)?.querySelector('select'),label);
      assert.ok(select.asElement(),'Expected field '+label);await select.asElement().select(value);await select.dispose();
    };
    await selectByLabel('Add step','github');await press(owner,'Insert step');
    await visible(owner,'Project lookup adapter');await selectByLabel('Lookup operation','issues');
    await capture(owner,'10-project-lookup-editor');
    await exec.emulateMediaFeatures([{name:'prefers-reduced-motion',value:'reduce'}]);
    assert.equal(await exec.$eval('.guided-review',n=>getComputedStyle(n).animationDuration),'0s');
    assert.deepEqual(audits.filter(a=>a.failures.length),[],'Automated accessibility checks found a regression');
    assert.deepEqual(errors,[]);assert.deepEqual(outside,[]);
    console.log(JSON.stringify({shots,clips,audits,externalRequests:outside.length,browserErrors:errors.length,result:'passed'}));
  }finally{await browser.close()}
}
main().catch(e=>{console.error(e.name+': '+e.message);process.exitCode=1});
