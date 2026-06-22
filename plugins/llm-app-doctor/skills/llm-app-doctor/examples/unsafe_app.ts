// Example TypeScript AI integration with hazards.
// Run: analyze_llm_code.ts -> analyze_llm_code.py unsafe_app.ts
import Anthropic from "@anthropic-ai/sdk";

const client = new Anthropic();

export async function answer(userMessage: string) {
  const response = await client.messages.create({
    model: "claude-3-opus-20240229",          // MOD001: retired -> 404
    max_tokens: 64000,                          // REL002: large, non-streaming
    // SEC002: untrusted input in the system prompt (template literal)
    system: `You are an assistant for ${userMessage}`,
    messages: [{ role: "user", content: userMessage }],
  });
  return response.content[0].text;
}
