// Source-level UI tests with a minimal DOM double; does not launch a browser.
// node tests/test_report_scripts.js LAB_REPORT.html NETWORK_REPORT.html
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

class Element {
  constructor(tag = '') { this.tag = tag; this.children = []; this.attributes = {}; this.handlers = {}; this.value = ''; this.textContent = ''; }
  append(child) { this.children.push(child); if (this.tag === 'select' && this.children.length === 1) this.value = child.value; }
  replaceChildren() { this.children = []; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(event, handler) { this.handlers[event] = handler; }
}

function boot(path, transform = null) {
  const html = fs.readFileSync(path, 'utf8');
  const dataMatch = html.match(/<script id="report-data" type="application\/json">([\s\S]*?)<\/script>/);
  const scriptMatch = html.match(/<script>"use strict";([\s\S]*?)<\/script>/);
  assert.ok(dataMatch && scriptMatch, 'Both data and executable script must exist');
  const data = JSON.parse(dataMatch[1]);
  if (transform) transform(data);
  const elements = new Map();
  for (const [_, tag, id] of html.matchAll(/<([a-z]+)[^>]*\bid="([^"]+)"/g)) elements.set(id, new Element(tag));
  elements.get('report-data').textContent = JSON.stringify(data);
  const document = {
    getElementById(id) { assert.ok(elements.has(id), `Unknown element ${id}`); return elements.get(id); },
    createElement(tag) { return new Element(tag); },
    createElementNS(ns, tag) { assert.equal(ns, 'http://www.w3.org/2000/svg'); return new Element(tag); },
  };
  vm.runInNewContext(scriptMatch[1], {document}, {timeout: 1000});
  return {data, elements};
}

const [labPath, networkPath] = process.argv.slice(2);
assert.ok(labPath && networkPath, 'Pass paths to generated lab and network HTML reports');
const lab = boot(labPath);
assert.equal(lab.elements.get('conditions').children.length, lab.data.injection_rates.length);
for (const scenario of Object.keys(lab.data.scenarios)) {
  for (const size of lab.data.sample_sizes) {
    for (const missing of lab.data.missing_rates) {
      lab.elements.get('scenario').value = scenario;
      lab.elements.get('size').value = String(size);
      lab.elements.get('missing').value = String(missing);
      lab.elements.get('scenario').handlers.change();
      assert.equal(lab.elements.get('conditions').children.length, lab.data.injection_rates.length);
      for (const element of lab.elements.get('curve').children) {
        for (const value of Object.values(element.attributes)) assert.ok(!/NaN|Infinity/.test(String(value)));
      }
    }
  }
}
const missing = boot(labPath, data => { for (const r of data.summaries) r.score = {mean:null, p025:null, p975:null, n_valid:0}; });
assert.equal(missing.elements.get('curve').children.filter(e => e.tag === 'circle').length, 0);
const network = boot(networkPath);
assert.equal(network.elements.get('pairs').children.length, network.data.pairs.length);
for (const target of network.data.targets) {
  network.elements.get('target').value = target.account;
  network.elements.get('target').handlers.change();
  assert.ok(network.elements.get('graph-note').textContent.includes('Showing'));
}
const empty = boot(networkPath, data => { data.targets=[];data.shared_followers=[];data.pairs=[];data.connected_target_groups=[];data.target_count=0;data.unique_follower_count=0;data.shared_follower_count=0;data.status='insufficient_accounts';data.as_of=null;data.snapshot_span_days=null; });
assert.ok(empty.elements.get('summary').textContent.startsWith('0 targets'));
console.log('Report script tests passed: all lab selector combinations, missing scores, target filters, and empty networks. No browser rendering was performed.');
