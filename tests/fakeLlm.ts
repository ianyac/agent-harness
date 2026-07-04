import type { Message, ToolCall } from "../src/llm.ts";

export type ScriptEntry =
  | { type: "text"; content: string }
  | {
      type: "tool_calls";
      calls: { name: string; arguments: Record<string, unknown> }[];
    };

export interface TurnRecord {
  output: ScriptEntry; // the scripted directive (recipe, not the dish)
  messages: Message[] | null; // what the model was shown; null until played
  tools: object[] | null; // what tool definitions were offered
}

export class FakeLlm {
  turns: TurnRecord[];
  currentLine = 0;
  #callCounter = 0;

  constructor(script: ScriptEntry[]) {
    this.turns = script.map((output) => ({
      output,
      messages: null,
      tools: null,
    }));
  }

  complete(messages: Message[], tools?: object[]): Message {
    const turn = this.turns[this.currentLine];
    if (turn === undefined) {
      throw new RangeError("FakeLlm script exhausted");
    }
    this.currentLine += 1;
    turn.messages = structuredClone(messages);
    turn.tools = tools === undefined ? null : structuredClone(tools);
    const entry = turn.output;
    switch (entry.type) {
      case "text":
        return { role: "assistant", content: entry.content };
      case "tool_calls":
        return {
          role: "assistant",
          content: null,
          tool_calls: entry.calls.map((c) =>
            this.#toolCall(c.name, c.arguments),
          ),
        };
      default:
        throw new Error(
          `unknown FakeLlm script entry type ${JSON.stringify((entry as { type: unknown }).type)}`,
        );
    }
  }

  #toolCall(name: string, args: Record<string, unknown>): ToolCall {
    return {
      id: `call_${this.#callCounter++}`,
      type: "function",
      function: { name, arguments: JSON.stringify(args) },
    };
  }
}
