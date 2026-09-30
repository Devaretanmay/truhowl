import { describe, it, expect } from 'vitest';
import { ChatOpenAI } from '../src/chat_models/openai';

describe('ChatOpenAI', () => {
  it('instantiates successfully with API key', () => {
    const model = new ChatOpenAI('test-api-key');
    expect(model).toBeDefined();
  });
});
