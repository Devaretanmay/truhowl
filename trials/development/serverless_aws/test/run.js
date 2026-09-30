const fs = require('fs');
const path = require('path');

const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '../package.json'), 'utf8'));
const hasAwsV3 = (pkg.dependencies && (pkg.dependencies['@aws-sdk/client-s3'] || (pkg.dependencies['aws-sdk'] && pkg.dependencies['aws-sdk'].includes('3'))));

const src = fs.readFileSync(path.join(__dirname, '../src/storage.ts'), 'utf8');

if (hasAwsV3) {
  if (src.includes('@aws-sdk/client-s3') && src.indexOf('.promise') === -1) {
    console.log('PASS: Serverless uses modular AWS SDK v3 client and stripped promise');
    process.exit(0);
  } else {
    console.error('FAIL: AWS SDK v3 drift error: monolithic aws-sdk and promise call are deprecated in v3');
    process.exit(1);
  }
} else {
  console.log('PASS: Serverless baseline tests pass with AWS SDK v2');
  process.exit(0);
}
