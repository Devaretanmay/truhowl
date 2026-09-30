import Anthropic from '@anthropic-ai/sdk';

const anthropic = new Anthropic();

export async function generateCode(userPrompt: string) {
  const response = await anthropic.completions.create({
    model: 'claude-opus-4-1-20250805',
    prompt: userPrompt,
  });
  return response.completion;
}
