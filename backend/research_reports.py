"""Standalone, offline report explorers. No CDN scripts or third-party dependencies."""
import html
import json
from pathlib import Path

from data_io import write_json

STYLE = '''
body{font:16px system-ui,sans-serif;background:#f4f7fa;color:#162b3c;margin:0}
main{max-width:1150px;margin:auto;padding:32px}h1{font-size:32px;margin-bottom:10px}
.note{padding:16px;background:#fff2d8;border-left:4px solid #bd7d13;line-height:1.5}
.panel{background:white;border:1px solid #d7e0e8;border-radius:10px;padding:20px;margin:20px 0}
.controls{display:flex;gap:24px;flex-wrap:wrap;margin:20px 0}label{display:grid;gap:6px}
select{padding:8px;font:inherit;max-width:400px}table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:10px;border-bottom:1px solid #dce4ec}th{background:#edf3f7}
.scroll{overflow:auto}svg{width:100%;height:auto;min-width:600px}p,li{line-height:1.5}
.muted{color:#50677a}.tag{background:#e0f1f1;padding:5px 10px;border-radius:5px}
'''


def safe_json(value):
    # Script data is untrusted: prevent closing the script element, including in handles.
    return json.dumps(value, ensure_ascii=True, allow_nan=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')


def page(title, body, data, script):
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>' + html.escape(title) + '</title><style>' + STYLE + '</style><main><h1>' + html.escape(title) + '</h1>'
            + body + '<div class="panel"><h2>Interpretation limits</h2><ul>'
            + ''.join('<li>' + html.escape(s) + '</li>' for s in data['limitations'])
            + '</ul></div></main><script id="report-data" type="application/json">' + safe_json(data)
            + '</script><script>"use strict";const data=JSON.parse(document.getElementById("report-data").textContent);'
            + script + '</script></html>')


COMMON_JS = '''
const fmt=(v,d=2)=>v===null||v===undefined?'unavailable':Number(v).toFixed(d);
const pct=v=>v===null||v===undefined?'unavailable':(100*v).toFixed(1)+'%';
const option=(select,value,label)=>{const o=document.createElement('option');o.value=value;o.textContent=label;select.append(o)};
const tableRow=(tbody,values)=>{const tr=document.createElement('tr');for(const v of values){const td=document.createElement('td');td.textContent=v;tr.append(td)}tbody.append(tr)};
const S=(tag,attrs,text)=>{const el=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [k,v] of Object.entries(attrs||{}))el.setAttribute(k,v);if(text!==undefined)el.textContent=text;return el};
'''


def validation_html(report):
    body = '''<p class="note"><strong>Synthetic validation only.</strong> These experiments test the existing heuristic on generated profiles. They do not measure real-world bot-detection accuracy.</p>
    <div class="controls"><label>Scenario<select id="scenario"></select></label><label>Sample size<select id="size"></select></label><label>Missing-field probability<select id="missing"></select></label></div>
    <div class="panel"><p id="scenario-description"></p><p id="run-info" class="muted"></p><div class="scroll"><svg id="curve" role="img" aria-label="Synthetic score response by injected fraction" viewBox="0 0 900 370"></svg></div>
    <p class="muted">Points show mean scores; whiskers show the 2.5th–97.5th percentile across simulated replicates. Dashed line: score 60. Gaps mean the score was unavailable.</p></div>
    <div class="panel scroll"><table><thead><tr><th>Injected fraction</th><th>Mean score</th><th>Replicate range</th><th>Scored / total</th><th>High-score fraction</th><th>Mean paired change</th><th>Changed component sets</th></tr></thead><tbody id="conditions"></tbody></table></div>'''
    script = COMMON_JS + '''
const sc=document.getElementById('scenario'),sz=document.getElementById('size'),mi=document.getElementById('missing');
for(const [key,value] of Object.entries(data.scenarios))option(sc,key,key.replaceAll('_',' ')+' · '+value.kind.replaceAll('_',' '));
for(const size of data.sample_sizes)option(sz,size,size);sz.value=data.sample_sizes[data.sample_sizes.length-1];
for(const missing of data.missing_rates)option(mi,missing,pct(missing));
function draw(){
 const rows=data.summaries.filter(r=>r.scenario===sc.value&&r.sample_size===Number(sz.value)&&r.missing_rate===Number(mi.value)).sort((a,b)=>a.injection_rate-b.injection_rate);
 document.getElementById('scenario-description').textContent=data.scenarios[sc.value].description;
 document.getElementById('run-info').textContent=data.replicates_per_condition+' replicates per condition · seed '+data.seed+' · reference time '+data.as_of;
 const svg=document.getElementById('curve');svg.replaceChildren();const x=v=>65+v*780,y=v=>305-v*2.6;
 for(let value=0;value<=100;value+=20){svg.append(S('line',{x1:65,x2:845,y1:y(value),y2:y(value),stroke:value===60?'#ba6b19':'#dce4ec','stroke-dasharray':value===60?'5 4':'none'}));svg.append(S('text',{x:52,y:y(value)+5,'text-anchor':'end',fill:'#50677a'},value))}
 for(const dose of data.injection_rates)svg.append(S('text',{x:x(dose),y:330,'text-anchor':'middle',fill:'#50677a'},pct(dose)));
 svg.append(S('text',{x:450,y:360,'text-anchor':'middle'},'Fraction of profiles assigned the scenario'));
 svg.append(S('text',{x:65,y:22},'Heuristic score (0–100)'));
 let segment=[];const flush=()=>{if(segment.length)svg.append(S('polyline',{points:segment.join(' '),fill:'none',stroke:'#087f8c','stroke-width':3}));segment=[]};
 for(const row of rows){if(row.score.mean===null){flush();continue}segment.push(x(row.actual_injection_rate)+','+y(row.score.mean))}flush();
 for(const row of rows){if(row.score.mean===null)continue;const px=x(row.actual_injection_rate);svg.append(S('line',{x1:px,x2:px,y1:y(row.score.p025),y2:y(row.score.p975),stroke:'#087f8c','stroke-width':2}));const dot=S('circle',{cx:px,cy:y(row.score.mean),r:5,fill:'#087f8c'});dot.append(S('title',{},pct(row.actual_injection_rate)+': '+fmt(row.score.mean)+' / 100'));svg.append(dot)}
 const tbody=document.getElementById('conditions');tbody.replaceChildren();
 for(const r of rows)tableRow(tbody,[pct(r.actual_injection_rate),fmt(r.score.mean),fmt(r.score.p025)+'–'+fmt(r.score.p975),r.score.n_valid+' / '+r.replicates,pct(r.high_score_rate)+' ('+r.high_score_count+'/'+r.high_score_denominator+')',fmt(r.paired_score_change.mean),r.component_set_changed_replicates+' / '+r.replicates]);
}
for(const control of [sc,sz,mi])control.addEventListener('change',draw);draw();
'''
    return page('Synthetic validation lab', body, report, script)


def network_html(report):
    body = '''<p class="note"><strong>Observed overlap, not proof of coordination.</strong> Each target uses its latest saved snapshot. The drawing shows shared followers only; the JSON graph includes every observed follower.</p>
    <div class="panel"><p id="summary"></p><p id="span" class="muted"></p><label>Highlight a target<select id="target"></select></label>
    <p id="graph-note" class="muted"></p><div class="scroll"><svg id="graph" role="img" aria-label="Bipartite graph linking target accounts to shared sampled followers"></svg></div></div>
    <div class="panel scroll"><h2>Pairwise overlap</h2><table><thead><tr><th>Target A</th><th>Target B</th><th>Shared / union</th><th>Jaccard</th><th>Overlap coefficient</th><th>New + low activity among shared</th><th>Display group edge</th></tr></thead><tbody id="pairs"></tbody></table></div>
    <div class="panel"><h2>Connected target groups</h2><p id="groups"></p><p class="muted">Groups are connected components of the thresholded target-overlap graph, not inferred coordination groups.</p></div>'''
    script = COMMON_JS + '''
const select=document.getElementById('target');option(select,'','All targets');for(const t of data.targets)option(select,t.account,'@'+t.account);
document.getElementById('summary').textContent=data.target_count+' targets · '+data.unique_follower_count+' unique observed followers · '+data.shared_follower_count+' observed in multiple samples';
document.getElementById('span').textContent=(data.status==='insufficient_accounts'?'Collect at least two different target accounts to compare overlap. ':'')+'Snapshot span: '+fmt(data.snapshot_span_days,3)+' days. Age reference: '+(data.as_of||'unavailable');
const tbody=document.getElementById('pairs');for(const p of data.pairs){const m=p.shared_profile_metrics.new_and_low_activity_rate;tableRow(tbody,['@'+p.left,'@'+p.right,p.shared_count+' / '+p.union_count,fmt(p.jaccard,4),fmt(p.overlap_coefficient,4),pct(m.value)+' ('+m.n_flagged+'/'+m.n_valid+' known)',p.passes_display_thresholds?'yes':'no'])}
document.getElementById('groups').textContent=data.connected_target_groups.length?data.connected_target_groups.map(g=>g.map(a=>'@'+a).join(' ↔ ')).join('; '):'No multi-target groups pass the selected thresholds.';
function draw(){
 const matching=data.shared_followers.filter(f=>!select.value||f.targets.includes(select.value));const followers=matching.slice(0,50),targets=data.targets.slice(0,30);
 document.getElementById('graph-note').textContent='Showing '+followers.length+' of '+matching.length+' matching shared followers and '+targets.length+' of '+data.targets.length+' targets. Drawing capped for readability; reports retain all data.';
 const svg=document.getElementById('graph'),height=Math.max(250,followers.length*30+70,targets.length*50+70);svg.replaceChildren();svg.setAttribute('viewBox','0 0 1100 '+height);
 const yTarget=new Map(targets.map((t,i)=>[t.account,45+i*(height-90)/Math.max(1,targets.length-1)]));
 for(let i=0;i<followers.length;i++){const f=followers[i],y=45+i*(height-90)/Math.max(1,followers.length-1);for(const target of f.targets){if(!yTarget.has(target))continue;svg.append(S('line',{x1:360,x2:620,y1:yTarget.get(target),y2:y,stroke:!select.value||target===select.value?'#57a8ad':'#dce4ec','stroke-width':1.4}))}const circle=S('circle',{cx:620,cy:y,r:5,fill:'#087f8c'});circle.append(S('title',{},f.identity));svg.append(circle);svg.append(S('text',{x:635,y:y+5,'font-size':12},String(f.username).slice(0,60)))}
 for(const target of targets){const y=yTarget.get(target.account);svg.append(S('circle',{cx:360,cy:y,r:8,fill:!select.value||select.value===target.account?'#243f59':'#9cabb8'}));const text=S('text',{x:345,y:y+5,'text-anchor':'end','font-size':12},'@'+target.account);text.append(S('title',{},target.sampled_unique_followers+' sampled followers'));svg.append(text)}
 if(!followers.length)svg.append(S('text',{x:620,y:120,fill:'#50677a'},'No shared followers in the selected samples.'));
}
select.addEventListener('change',draw);draw();
'''
    return page('Shared-audience network', body, report, script)


def export_validation(out, report, trials):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / 'validation.json', report)
    with (out / 'trials.jsonl').open('w', encoding='utf-8') as file:
        for trial in trials:
            file.write(json.dumps(trial, ensure_ascii=False, allow_nan=False) + '\n')
    (out / 'report.html').write_text(validation_html(report), encoding='utf-8')
    lines = ['# Synthetic validation lab', '', 'All data is generated. No real-world accuracy is claimed.', '',
             f"{report['trial_count']} trials; seed {report['seed']}; {report['replicates_per_condition']} replicates per condition.", '',
             'Open report.html to select sample size, missingness, and scenario. All conditions are in validation.json; individual trials are in trials.jsonl.', '',
             '## Generator', '', report['generator'], '', '## Interpretation limits', '']
    lines += ['- ' + v for v in report['limitations']]
    (out / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def export_network(out, report, graph):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / 'network.json', report)
    write_json(out / 'graph.json', graph)
    (out / 'report.html').write_text(network_html(report), encoding='utf-8')
    lines = ['# Shared-audience network', '',
             f"Targets: {report['target_count']}; unique sampled followers: {report['unique_follower_count']}; shared followers: {report['shared_follower_count']}.",
             f"Status: {report['status']}; reference time: {report['as_of']}; snapshot span: {report['snapshot_span_days']} days.", '',
             '| Target A | Target B | Shared | Union | Jaccard |', '|---|---|---:|---:|---:|']
    for pair in report['pairs']:
        # Escape Markdown table delimiters; raw names are retained in JSON.
        left, right = (pair[k].replace('|', '\\|').replace('\n', ' ') for k in ('left', 'right'))
        jaccard = 'unavailable' if pair['jaccard'] is None else f"{pair['jaccard']:.6f}"
        lines.append(f"| {left} | {right} | {pair['shared_count']} | {pair['union_count']} | {jaccard} |")
    lines += ['', '## Interpretation limits', ''] + ['- ' + v for v in report['limitations']]
    (out / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
