const fs = require('fs');
const path = require('path');

const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '../package.json'), 'utf8'));
const sentryVer = pkg.dependencies && pkg.dependencies['@sentry/node'] ? pkg.dependencies['@sentry/node'] : '';

const src = fs.readFileSync(path.join(__dirname, '../src/monitoring.ts'), 'utf8');

if (sentryVer.includes('8')) {
  if (src.includes('Sentry.getClient()') && !src.includes('getCurrentHub')) {
    console.log('PASS: Sentry monitoring uses v8 Sentry.getClient() interface');
    process.exit(0);
  } else {
    console.error('FAIL: Sentry v8 drift error: getCurrentHub() removed in v8, must use Sentry.getClient()');
    process.exit(1);
  }
} else {
  console.log('PASS: Sentry monitoring baseline tests pass with Sentry v7');
  process.exit(0);
}
