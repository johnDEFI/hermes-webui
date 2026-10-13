// Execute the actual navigation helpers in a VM, without a server or jsdom.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const sessions=fs.readFileSync('static/sessions.js','utf8');
const panels=fs.readFileSync('static/panels.js','utf8');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
(async()=>{
  const events=[], requests=[],rows=['a','b'].map(sid=>({dataset:{sid},classList:{toggle(){},remove(){}}}));
  const ctx={S:{activeProfile:'default'},_showAllProfiles:true,_loadSessionGeneration:0,_loadingSessionId:null,
    document:{querySelectorAll:()=>rows},_profileMatchesActiveProfile:(a,b)=>a===b,
    _isExternalSession:()=>true,_externalImportPayload:s=>s,
    api:(url,opts)=>{const d=deferred();requests.push({url,opts,d});events.push('import');return d.promise;},
    loadSession:async sid=>{events.push('messages:'+sid);},
    renderSessionListFromCache:()=>events.push('cache'),
    _profileSwitchGeneration:0,_sessionListSkeletonActive:true,
    _setProfileSwitchListEmbargo:()=>events.push('embargo'),
    renderSessionList:()=>{events.push('list');return new Promise(()=>{});},
    _profileSwitchPanelLoad:()=>{events.push('panels');return new Promise(()=>{});},
    _refreshProfileSwitchBackground:()=>events.push('background')};
  vm.createContext(ctx);
  const start=panels.indexOf('let _profileSwitchTail =');
  const end=panels.indexOf('async function _switchToProfileNow',start);
  vm.runInContext(panels.slice(start,end),ctx);
  const switchRequests=[];
  ctx._switchToProfileNow=(name,opts)=>{
    const d=deferred();switchRequests.push({name,opts,d});
    return d.promise.then(()=>{ctx.S.activeProfile=name;ctx._profileSwitchGeneration++;
      if(opts.openingExistingSession)vm.runInContext('_deferredSidebarProfileRefresh=_profileSwitchGeneration',ctx);
      return true;});
  };
  const navStart=sessions.indexOf('function _sidebarSessionProfileName');
  const navEnd=sessions.indexOf('function _isReadOnlySession',navStart);
  vm.runInContext(sessions.slice(navStart,navEnd),ctx);
  const a={session_id:'a',profile:'other'},b={session_id:'b',profile:'third'};
  const first=ctx._openSidebarSession(a),same=ctx._openSidebarSession(a);
  assert.strictEqual(first,same);
  assert.strictEqual(requests.length,1);
  requests[0].d.resolve({});await tick();
  assert.strictEqual(switchRequests.length,1);
  assert.strictEqual(switchRequests[0].opts.openingExistingSession,true);
  assert(!events.includes('list'));
  switchRequests[0].d.resolve();await first;
  assert(events.indexOf('messages:a')<events.indexOf('list'));
  assert(events.indexOf('messages:a')<events.indexOf('panels'));
  // Different targets serialize; only the tail shares, preserving A/B/A intent.
  const pa=ctx.switchToProfile('A'),pa2=ctx.switchToProfile('A');assert.strictEqual(pa,pa2);
  const pb=ctx.switchToProfile('B'),pa3=ctx.switchToProfile('A');
  await tick();assert.strictEqual(switchRequests.length,2);
  switchRequests[1].d.resolve();await pa;await tick();
  assert.strictEqual(switchRequests[2].name,'B');switchRequests[2].d.resolve();await pb;await tick();
  assert.strictEqual(switchRequests[3].name,'A');switchRequests[3].d.resolve();await pa3;
  assert.strictEqual(ctx.S.activeProfile,'A');
  // Superseded imports cannot open an older conversation.
  const old=ctx._openSidebarSession(a),latest=ctx._openSidebarSession(b);
  const tail=requests.length;
  requests[tail-2].d.resolve({});await old;
  assert(!events.includes('messages:b'));
  requests[tail-1].d.resolve({});await tick();
  switchRequests.at(-1).d.resolve();await latest;
  assert.strictEqual(events.filter(x=>x.startsWith('messages:')).at(-1),'messages:b');
  // Shared import errors are evicted, so retry issues a fresh request.
  const error=ctx._importExternalSession(a),error2=ctx._importExternalSession(a);assert.strictEqual(error,error2);
  requests.at(-1).d.reject(new Error('test'));await error.catch(()=>{});
  const retry=ctx._importExternalSession(a);assert.notStrictEqual(error,retry);
  requests.at(-1).d.resolve({});await retry;
  console.log('client navigation regression checks passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
