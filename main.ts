import type { LLMClient, Message } from "./src/llm.ts";
// TEMP: REPL runs on the fake until the codex adapter lands (TS-2)
import { FakeLlm } from "./tests/fakeLlm.ts";

export function runTurn(
  messages: Message[],
  userInput: string,
  llm: LLMClient,
): Message {
  messages.push({ role: "user", content: userInput });
  const reply = llm.complete(messages);
  messages.push(reply);
  return reply;
}

function main(): void {
  const llm = new FakeLlm([
    { type: "text", content: "Hello! How can I help you?" },
    { type: "text", content: "Goodbye!" },
  ]);
  const messages: Message[] = [];
  while (true) {
    const userInput = prompt("You:");
    if (userInput === null) break; // EOF (Ctrl-D)
    const reply = runTurn(messages, userInput, llm);
    console.log("agent:", reply.content);
  }
}

if (import.meta.main) {
  main();
}
