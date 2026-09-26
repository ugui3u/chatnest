// Exercise the actual frontend helper without browser or npm dependencies.
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');
const html = readFileSync(join(__dirname, '../static/index.html'), 'utf8');
const helper = html.split('\n').find(line => line.startsWith('function _cpy('));
async function run() {
  let copied, removed = false, selected = false;
  const ta = {style: {}, select() { selected = true; }, remove() { removed = true; }};
  const context = {navigator: {}, document: {
    createElement() {return ta;}, body: {append() {}},
    execCommand(command) { assert.equal(command, 'copy'); copied = ta.value; return true; },
  }};
  vm.createContext(context); vm.runInContext(helper, context);
  await context._cpy('fallback text');
  assert.equal(copied, 'fallback text'); assert.ok(selected && removed);
  context.document.execCommand = () => false; removed = false;
  await assert.rejects(context._cpy('failure')); assert.ok(removed);
  context.navigator.clipboard = {writeText: async text => {copied = text;}};
  await context._cpy('native text'); assert.equal(copied, 'native text');
  console.log('PASS: clipboard native, HTTP fallback, failure cleanup');
}
run().catch(error => {console.error(error); process.exitCode = 1;});
