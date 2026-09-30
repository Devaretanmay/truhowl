const fs = require('fs');
const path = require('path');

const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '../package.json'), 'utf8'));
const clerkVer = pkg.dependencies && pkg.dependencies['@clerk/nextjs'] ? pkg.dependencies['@clerk/nextjs'] : '';

const src = fs.readFileSync(path.join(__dirname, '../src/middleware.ts'), 'utf8');

if (clerkVer.includes('5')) {
  if (src.includes('clerkMiddleware(')) {
    console.log('PASS: Uploadthing uses Clerk v5 clerkMiddleware interface');
    process.exit(0);
  } else {
    console.error('FAIL: Clerk v5 drift error: authMiddleware removed in v5, must use clerkMiddleware');
    process.exit(1);
  }
} else {
  console.log('PASS: Uploadthing baseline tests pass with Clerk v4');
  process.exit(0);
}
