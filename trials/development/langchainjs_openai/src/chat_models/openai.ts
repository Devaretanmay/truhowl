import { Configuration, OpenAIApi } from 'openai';

export interface ChatOpenAICallOptions {
  model?: string;
  temperature?: number;
}

export class ChatOpenAI {
  private client: OpenAIApi;

  constructor(apiKey: string) {
    const config = new Configuration({ apiKey });
    this.client = new OpenAIApi(config);
  }

  async call(messages: Array<{ role: string; content: string }>, options?: ChatOpenAICallOptions) {
    const response = await this.client.createChatCompletion({
      model: options?.model || 'gpt-3.5-turbo',
      messages: messages as any,
      temperature: options?.temperature ?? 0.7,
    });
    return response.data.choices[0].message;
  }
}
