# TypeScript & Node.js Native SDK (`@truhowl/sdk`)

> **Upcoming Roadmap:** The `@truhowl/sdk` npm package distribution (TypeScript / Node.js native bindings via NAPI-RS) is planned for upcoming distribution.

The TypeScript SDK provides high-performance Node.js bindings to the Truhowl Rust core via NAPI-RS.

---

## 1. Installation

```bash
npm install @truhowl/sdk
```

---

## 2. API Reference & Native Functions

### `sandboxSupported(): boolean`
Returns `true` if kernel sandboxing (Seatbelt on macOS or Landlock on Linux) is supported by the current OS kernel.

```ts
import { sandboxSupported } from '@truhowl/sdk'

if (sandboxSupported()) {
  console.log('Kernel sandbox is available.')
}
```

---

### `runtimeCheckPermission(policyJson: string, checkPermissionsJson: string): boolean`
Evaluates a JSON policy against requested permissions.

```ts
import { runtimeCheckPermission } from '@truhowl/sdk'

const allowed = runtimeCheckPermission(
  JSON.stringify({ permissions: ['fs_read'] }),
  JSON.stringify(['fs_read'])
)
console.log(allowed) // true
```

---

### `runtimeCheckCommand(command: string): boolean`
Checks a command string against the native blocklist.

```ts
import { runtimeCheckCommand } from '@truhowl/sdk'

console.log(runtimeCheckCommand('ls -la'))     // true
console.log(runtimeCheckCommand('rm -rf /'))   // false (blocked)
```

---

### `Runtime` Class

Manages multi-compartment execution topology and routing order in TypeScript:

```ts
import { Runtime } from '@truhowl/sdk'

const configs = JSON.stringify({
  fetch: { permissions: ['fs_read'] },
  build: { permissions: ['fs_read', 'fs_write'] }
})

const edges = JSON.stringify([['fetch', 'build']])

const rt = new Runtime(configs, edges)

console.log(rt.names())                   // ['fetch', 'build']
console.log(rt.runOrder())                // ['fetch', 'build']
console.log(rt.canRoute('fetch', 'build')) // true
```
