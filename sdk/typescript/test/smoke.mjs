import assert from 'node:assert/strict'
import truhowl from '../index.js'

assert.ok(typeof truhowl.version() === 'string' && truhowl.version().length > 0, 'version() returns a string')
console.log('version:', truhowl.version())

assert.ok(typeof truhowl.sandboxSupported() === 'boolean', 'sandboxSupported() returns boolean')
console.log('sandboxSupported:', truhowl.sandboxSupported())

const original = 'the quick brown fox jumps over the lazy dog. '.repeat(10)
const compressed = truhowl.compress(original)
assert.ok(typeof compressed === 'string' && compressed.length > 0, 'compress() returns a string')
console.log(`compress: ${original.length} bytes -> ${compressed.length} bytes`)

// --- Compartment runtime ---

const policy = JSON.stringify({ permissions: ['fs_read', 'fs_write'] })
assert.ok(truhowl.runtimeCheckPermission(policy, JSON.stringify(['fs_read'])) === true, 'fs_read allowed')
assert.ok(truhowl.runtimeCheckPermission(policy, JSON.stringify(['network'])) === false, 'network denied')
console.log('runtimeCheckPermission: allowed/denied ok')

assert.ok(truhowl.runtimeCheckCommand('rm -rf /') === false, 'rm -rf / blocked')
assert.ok(truhowl.runtimeCheckCommand('grep foo file.txt') === true, 'grep allowed')
console.log('runtimeCheckCommand: blocklist ok')

const configs = JSON.stringify({ configs: [{ name: 'a', allow_outbound_to: ['b'] }, { name: 'b' }] })
assert.ok(truhowl.runtimeValidate(configs, JSON.stringify([['a', 'b']])) === true, 'runtime valid')
assert.ok(truhowl.runtimeValidate(configs, JSON.stringify([['a', 'nope']])) === false, 'runtime invalid')
console.log('runtimeValidate: ok')

assert.ok(truhowl.runtimeCanRoute(configs, 'a', 'b') === true, 'a->b allowed')
assert.throws(() => truhowl.runtimeCanRoute(configs, 'zzz', 'b'), 'unknown compartment throws')
console.log('runtimeCanRoute: ok')

// Opaque handle - parses configs once, routes many times.
// Uses its own configs (with a registered `c`) so denial + runOrder
// assertions exercise real whitelist/order semantics.
const handleConfigs = JSON.stringify({
  configs: [
    { name: 'a', allow_outbound_to: ['b'] },
    { name: 'b' },
    { name: 'c', allow_inbound_from: [] },
  ],
})
const rt = new truhowl.Runtime(handleConfigs, JSON.stringify([['a', 'b']]))
assert.ok(rt.canRoute('a', 'b') === true, 'handle a->b allowed')
assert.ok(rt.canRoute('b', 'a') === true, 'handle b->a allowed (wildcard default)')
assert.ok(rt.canRoute('b', 'c') === false, 'handle b->c denied')
assert.throws(() => rt.canRoute('zzz', 'b'), 'handle unknown compartment throws')
const order = rt.runOrder()
assert.ok(Array.isArray(order) && order.length === 3 && order[0] === 'a' && order[2] === 'c', `runOrder: ${JSON.stringify(order)}`)
const orderB = rt.runOrder('b')
assert.ok(orderB.length === 2 && orderB[0] === 'b' && orderB[1] === 'c', `runOrder(b): ${JSON.stringify(orderB)}`)
assert.ok(rt.names().length === 3, 'names() returns registered compartments')
console.log('Runtime handle: ok')

const routes = JSON.stringify([{ prefix: '/openai', upstream: 'https://api.openai.com' }])
assert.ok(truhowl.runtimeCredentialRewrite(routes, '/openai/v1/chat') === 'https://api.openai.com/v1/chat', 'route rewrite')
assert.ok(truhowl.runtimeCredentialRewrite(routes, '/anthropic/v1') === null, 'no match -> null')
console.log('runtimeCredentialRewrite: ok')

process.env.BW_TS_TEST_KEY = 'sk-test'
try {
  assert.ok(truhowl.runtimeCredentialResolve('env:BW_TS_TEST_KEY') === 'sk-test', 'env credential resolve')
  assert.ok(truhowl.runtimeCredentialResolve('env:BW_TS_MISSING') === '', 'missing env -> empty')
} finally {
  delete process.env.BW_TS_TEST_KEY
}
console.log('runtimeCredentialResolve: ok')

const snapWork = `/tmp/bw_ts_snap_${Date.now()}/work`
const snapDir = `/tmp/bw_ts_snap_${Date.now()}/.snapshots`
const { mkdirSync, writeFileSync, readFileSync, rmSync } = await import('node:fs')
mkdirSync(snapWork, { recursive: true })
writeFileSync(`${snapWork}/a.txt`, 'hello')
assert.ok(truhowl.runtimeSnapshot(snapWork, snapDir) === 1, 'snapshot count 1')
writeFileSync(`${snapWork}/a.txt`, 'changed')
assert.ok(truhowl.runtimeRestore(snapWork, snapDir) === 1, 'restore count 1')
assert.ok(readFileSync(`${snapWork}/a.txt`, 'utf8') === 'hello', 'content restored')
rmSync(snapWork, { recursive: true, force: true })
rmSync(snapDir, { recursive: true, force: true })
console.log('runtimeSnapshot/runtimeRestore: ok')

console.log('\nAll TypeScript SDK smoke tests passed.')
