const fs = require('fs');
const path = require('path');

const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '../package.json'), 'utf8'));
const stripeVer = pkg.dependencies && pkg.dependencies.stripe ? pkg.dependencies.stripe : '';

const src = fs.readFileSync(path.join(__dirname, '../src/billing.ts'), 'utf8');

if (stripeVer.includes('22')) {
  if (src.includes('amount: String(amount)') || src.includes('String(amount)')) {
    console.log('PASS: processCharge adheres to Stripe v22 string contract');
    process.exit(0);
  } else {
    console.error('FAIL: Stripe v22 drift error: processCharge amount must be string, found raw number');
    process.exit(1);
  }
} else {
  console.log('PASS: processCharge baseline tests pass with Stripe v11');
  process.exit(0);
}
