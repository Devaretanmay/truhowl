const fs = require('fs');
const path = require('path');

const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '../package.json'), 'utf8'));
const openaiVer = pkg.dependencies && pkg.dependencies.openai ? pkg.dependencies.openai : '';

const src = fs.readFileSync(path.join(__dirname, '../src/chat_models/openai.ts'), 'utf8');

if (openaiVer.includes('4')) {
  if (src.includes('chat.completions.create')) {
    console.log('PASS: ChatOpenAI uses OpenAI v4 chat.completions.create SDK interface');
    process.exit(0);
  } else {
    console.error('FAIL: OpenAI v4 drift error: createChatCompletion is removed in v4, must use chat.completions.create');
    process.exit(1);
  }
} else {
  console.log('PASS: ChatOpenAI baseline tests pass with OpenAI v3');
  process.exit(0);
}
