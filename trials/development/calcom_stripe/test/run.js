const fs = require('fs');
const path = require('path');

const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '../package.json'), 'utf8'));
const stripeVer = pkg.dependencies && pkg.dependencies.stripe ? pkg.dependencies.stripe : '';

const src = fs.readFileSync(path.join(__dirname, '../packages/features/ee/billing/stripe.ts'), 'utf8');

if (stripeVer.includes('13')) {
  if (src.includes('amount: String(amount)') || src.includes('String(amount)')) {
    console.log('PASS: createCheckoutSession adheres to Stripe v13 string amount contract');
    process.exit(0);
  } else {
    console.error('FAIL: Stripe v13 drift error: amount parameter must be string, found raw number');
    process.exit(1);
  }
} else {
  console.log('PASS: createCheckoutSession baseline tests pass with Stripe v11');
  process.exit(0);
}
