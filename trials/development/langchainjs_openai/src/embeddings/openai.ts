import { Configuration, OpenAIApi } from 'openai';

export class OpenAIEmbeddings {
  private client: OpenAIApi;

  constructor(apiKey: string) {
    const config = new Configuration({ apiKey });
    this.client = new OpenAIApi(config);
  }

  async embedQuery(text: string) {
    const response = await this.client.createEmbedding({
      model: 'text-embedding-ada-002',
      input: text,
    });
    return response.data.data[0].embedding;
  }
}
