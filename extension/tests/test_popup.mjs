import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import {normalizeProfile} from '../logic.mjs';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const html=fs.readFileSync(path.join(root,'popup.html'),'utf8');
const script=fs.readFileSync(path.join(root,'popup.js'),'utf8').replace(/^import .*?;\n/,'');
const fixture={account:'@alice@example.org',severity:'moderate',score:57.255,sample_size:287,presentation_version:2,stats_analysis:{goodness_of_fit:14.029288758264371,adjusted_p_value:.0810052026,following_imbalance:.49,low_activity:23/30},
 gemini_reason:'Quiet accounts and sparse profiles contribute most to this assessment.',summary_source:'Gemini + Python rating'};
class Element{
 constructor(){this.handlers={};this.hidden=false;this.value='';this.disabled=false;this.style={};this.textContent='';}
 setAttribute(k,v){this[k]=v;}addEventListener(t,f){this.handlers[t]=f;}focus(){this.focused=true;}
}
async function mount(storage={},configured=true,initialState='complete',initialStep=0){
 const elements=new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>[m[1],new Element()]));
 for(const [,id] of html.matchAll(/id="([^"]+)"[^>]*\bhidden/g))elements.get(id).hidden=true;
 const get=id=>{assert(elements.has(id),id);return elements.get(id);};
 let state=initialState,step=initialStep,offline=false,nextTimer,code=200;const requests=[];
 const local={setAccessLevel:async()=>{},get:async keys=>Object.fromEntries((Array.isArray(keys)?keys:[keys]).map(k=>[k,storage[k]])),set:async data=>Object.assign(storage,data),remove:async key=>{delete storage[key];}};
 const context={document:{getElementById:get},chrome:{storage:{local},tabs:{query:async()=>[{url:'https://mastodon.social/@alice@example.org'}]}},normalizeProfile,AbortSignal,
 setTimeout:fn=>{nextTimer=fn;return 1;},clearTimeout:()=>{nextTimer=null;},fetch:async(url,options)=>{
  requests.push({url,options});if(offline)throw Error('offline');
  return {ok:code===200,status:code,json:async()=>{
   if(code!==200)return {error:'Connection code rejected'};
   if(url.endsWith('/health'))return {service:'detest',api_version:2,gemini_available:configured,model:'gemini-3.8-flash'};
   if(url.endsWith('/settings')){configured=true;return {gemini_available:true,model:'gemini-3.8-flash'};}
   return {id:'a'.repeat(32),state:options.method==='POST'?'running':state,stage:['Reading followers','Analysing patterns','Writing explanation'][step],step,result:fixture,error:state==='explanation_failed'?'Gemini quota reached':'Hidden follower network'};
  }};
 }};
 vm.runInNewContext(script,context);
 const flush=async()=>{for(let i=0;i<100;i++)await Promise.resolve();};await flush();
 return {get,storage,requests,flush,state:v=>{state=v;},step:v=>{step=v;},status:v=>{code=v;},offline:v=>{offline=v;},tick:async()=>{const fn=nextTimer;nextTimer=null;if(fn)await fn();},event:async(id,type='click')=>{await get(id).handlers[type]({preventDefault(){}});await flush();}};
}
for(const profile of ['@alice@example.org','alice@example.org','https://example.org/@alice','https://mastodon.social/@alice@example.org'])assert.equal(normalizeProfile(profile),'@alice@example.org');
const app=await mount({},false);assert(app.get('settings').open);
app.get('token').value='connection-code';await app.event('connect');assert.equal(app.get('connection').textContent,'Connected');
await app.event('current-tab');assert.equal(app.get('profile').value,'@alice@example.org');
await app.event('analyze-form','submit');assert(app.get('settings').open);assert(!app.requests.some(r=>r.url.endsWith('/jobs')));
app.get('api-key').value='test-key-never-store-in-chrome';app.status(400);await app.event('key-form','submit');assert.equal(app.get('api-key').value,'test-key-never-store-in-chrome');assert.equal(app.get('api-key').disabled,false);assert(!JSON.stringify(app.storage).includes('test-key'));app.status(200);await app.event('key-form','submit');assert.equal(app.get('api-key').value,'');assert(!JSON.stringify(app.storage).includes('test-key'));
await app.event('analyze-form','submit');assert.equal(app.get('rating').textContent,'Moderate bot risk');assert.equal(app.get('summary').textContent,fixture.gemini_reason);assert(!app.get('result').hidden);
const submitted=app.requests.find(r=>r.url.endsWith('/jobs')&&r.options.method==='POST');assert.deepEqual(JSON.parse(submitted.options.body),{account:'@alice@example.org',mode:'fresh',use_gemini:true});
const reopened=await mount(app.storage);assert.equal(reopened.get('summary').textContent,fixture.gemini_reason);
const legacy=await mount({lastResult:{...fixture,presentation_version:1,gemini_reason:undefined,summary:'Old fallback'}});assert(legacy.get('result').hidden);
app.state('running');await app.event('analyze-form','submit');assert(app.get('analyze').disabled);assert(app.storage.activeJob);
const resumed=await mount(app.storage);assert.equal(resumed.get('summary').textContent,fixture.gemini_reason);
assert.equal(app.get('step-0').className,'step active');assert.equal(app.get('analyze').textContent,'Analysing…');
app.step(1);await app.tick();assert.equal(app.get('node-0').textContent,'✓');assert.equal(app.get('step-1').className,'step active');assert.equal(app.get('step-2').className,'step pending');
const inProgress=await mount({...app.storage,activeJob:'a'.repeat(32)},true,'running',1);assert.equal(inProgress.get('step-0').className,'step completed');assert.equal(inProgress.get('step-1').className,'step active');
app.step(2);await app.tick();assert.equal(app.get('node-1').textContent,'✓');assert.equal(app.get('step-2').className,'step active');
app.state('explanation_failed');await app.tick();assert(app.get('result').hidden);assert(!app.get('retry').hidden);assert.equal(app.get('error').textContent,'Gemini quota reached');
assert.equal(app.get('step-2').className,'step failed');assert.equal(app.get('node-0').textContent,'✓');
app.state('complete');await app.event('retry');assert(app.requests.some(r=>r.url.endsWith('/retry')));assert.equal(app.get('summary').textContent,fixture.gemini_reason);
assert.equal(app.get('score').textContent,'57.3');assert.equal(app.get('risk-marker').style.left,'57.255%');assert.equal(app.get('risk-scale')['aria-valuenow'],'57.255');assert.equal(app.get('sample-count').textContent,'287 followers analysed');
assert.equal(app.get('step-2').className,'step completed');assert.equal(app.get('analyze').textContent,'Analyse');
const failed=await mount({token:'connection-code',activeJob:'a'.repeat(32)},true,'failed',0);assert.equal(failed.get('step-0').className,'step failed');assert.equal(failed.get('step-1').className,'step pending');assert(failed.get('result').hidden);
const insufficient=await mount({lastResult:{...fixture,score:null,severity:'insufficient_data',gemini_reason:null,sample_size:4}});assert(insufficient.get('score-panel').hidden);assert.equal(insufficient.get('rating').textContent,'Not enough data');
for(const score of [0,100]){const edge=await mount({lastResult:{...fixture,score}});assert(!edge.get('score-panel').hidden);assert.equal(edge.get('risk-marker').style.left,score+'%');}
assert(html.includes('<summary>stats analysis (FOR THE NERDS)</summary>'));
assert(!app.get('stats-analysis').hidden);assert.equal(app.get('stat-fit').textContent,'14.03');assert.equal(app.get('stat-p').textContent,'0.081');assert.equal(app.get('stat-ratio').textContent,'49.0%');assert.equal(app.get('stat-activity').textContent,'76.7%');
const oldStats=await mount({lastResult:{...fixture,stats_analysis:undefined}});assert(oldStats.get('stats-analysis').hidden);
const unavailableStats=await mount({lastResult:{...fixture,stats_analysis:{goodness_of_fit:null,adjusted_p_value:null,following_imbalance:0,low_activity:null}}});assert.equal(unavailableStats.get('stat-fit').textContent,'Not available');assert.equal(unavailableStats.get('stat-p').textContent,'Not available');assert.equal(unavailableStats.get('stat-ratio').textContent,'0.0%');assert.equal(unavailableStats.get('stat-activity').textContent,'Not available');
const tinyP=await mount({lastResult:{...fixture,stats_analysis:{...fixture.stats_analysis,adjusted_p_value:1e-12}}});assert.equal(tinyP.get('stat-p').textContent,'1.00e-12');
const floorP=await mount({lastResult:{...fixture,stats_analysis:{...fixture.stats_analysis,adjusted_p_value:1e-300,p_value_at_floor:true}}});assert.equal(floorP.get('stat-p').textContent,'≤ 1e-300');
app.offline(true);await app.event('analyze-form','submit');assert(app.get('error').textContent.includes('DeTect is offline'));
assert.equal(app.get('step-0').className,'step paused');
assert(html.includes('Up to 300 public followers'));assert(html.includes('>Analyse</button>'));
for(const banned of ['Who’s in the audience','AUDIENCE ASSESSMENT','Statistical rules','hypothesis tests','Patterns are clues','type="checkbox"','class="lens"','id="date"','id="account"'])assert(!html.includes(banned),banned);
assert(html.includes('<summary>Connection</summary>'));assert(html.includes('<h1>DeTect</h1>'));
const manifest=JSON.parse(fs.readFileSync(path.join(root,'manifest.json')));assert.equal(manifest.name,'DeTect');assert.equal(manifest.manifest_version,3);assert(!manifest.icons);
console.log('PASS: DeTect clean UI, connection and key setup, no key in Chrome storage, automatic fresh/Gemini request, real explanation rendering, legacy fallback suppression, job recovery, explanation-only retry and offline state. Score and sample size, sequential progress, failure/pause/retry and insufficient data verified with DOM doubles.');
