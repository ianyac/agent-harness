import { expect, test } from "bun:test";

import type { Message } from "../src/llm.ts";
import { runTurn } from "../main.ts";
import { FakeLlm } from "./fakeLlm.ts";

test("fake llm returns scripted responses in order", () => {
  const llm = new FakeLlm([
    { type: "text", content: "first" },
    { type: "text", content: "second" },
  ]);
  expect(llm.complete([{ role: "user", content: "hi" }])).toEqual({
    role: "assistant",
    content: "first",
  });
  expect(llm.complete([{ role: "user", content: "again" }])).toEqual({
    role: "assistant",
    content: "second",
  });
});

test("fake llm records what it was shown", () => {
  const llm = new FakeLlm([{ type: "text", content: "ok" }]);
  llm.complete([{ role: "user", content: "hi" }]);
  expect(llm.turns[0]!.messages).toEqual([{ role: "user", content: "hi" }]);
});

test("runTurn appends user and assistant messages", () => {
  const llm = new FakeLlm([{ type: "text", content: "hello there" }]);
  const messages: Message[] = [];
  const reply = runTurn(messages, "hi", llm);
  expect(messages).toEqual([
    { role: "user", content: "hi" },
    { role: "assistant", content: "hello there" },
  ]);
  expect(reply).toEqual({ role: "assistant", content: "hello there" });
});

test("model sees full history each turn", () => {
  const llm = new FakeLlm([
    { type: "text", content: "a" },
    { type: "text", content: "b" },
  ]);
  const messages: Message[] = [];
  runTurn(messages, "one", llm);
  runTurn(messages, "two", llm);
  expect(llm.turns[1]!.messages).toEqual([
    { role: "user", content: "one" },
    { role: "assistant", content: "a" },
    { role: "user", content: "two" },
  ]);
});
