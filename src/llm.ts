// Internal message format = OpenAI chat format, snake_case and all —
// the Stage 1 decision survives the language pivot.

export interface ToolCall {
  id: string;
  type: "function";
  function: {
    name: string;
    arguments: string; // stays a JSON string end to end — the accepted wart
  };
}

export interface Message {
  role: "user" | "assistant" | "system" | "tool";
  content: string | null;
  tool_calls?: ToolCall[];
  tool_call_id?: string;
}

export interface LLMClient {
  complete(messages: Message[], tools?: object[]): Message;
}
