import { expect, test } from "bun:test";

import type { ScriptEntry } from "./fakeLlm.ts";
import { FakeLlm } from "./fakeLlm.ts";

test("single call entry becomes a tool_calls message", () => {
  const llm = new FakeLlm([
    { type: "tool_calls", calls: [{ name: "add", arguments: { a: 2, b: 3 } }] },
  ]);
  const reply = llm.complete([{ role: "user", content: "sum these" }]);
  expect(reply.role).toBe("assistant");
  expect(reply.content).toBeNull();
  expect(reply.tool_calls).toHaveLength(1);
  const call = reply.tool_calls![0]!;
  expect(call.id).toBe("call_0");
  expect(call.type).toBe("function");
  expect(call.function.name).toBe("add");
  expect(JSON.parse(call.function.arguments)).toEqual({ a: 2, b: 3 });
});

test("call ids stay unique across turns", () => {
  const llm = new FakeLlm([
    { type: "tool_calls", calls: [{ name: "add", arguments: { a: 1, b: 1 } }] },
    { type: "tool_calls", calls: [{ name: "add", arguments: { a: 2, b: 2 } }] },
  ]);
  const first = llm.complete([]).tool_calls![0]!.id;
  const second = llm.complete([]).tool_calls![0]!.id;
  expect(first).not.toBe(second);
});

test("multiple calls in one entry share the message", () => {
  const llm = new FakeLlm([
    {
      type: "tool_calls",
      calls: [
        { name: "add", arguments: { a: 1, b: 1 } },
        { name: "add", arguments: { a: 2, b: 2 } },
      ],
    },
  ]);
  const reply = llm.complete([]);
  expect(reply.tool_calls).toHaveLength(2);
  const ids = new Set(reply.tool_calls!.map((c) => c.id));
  expect(ids.size).toBe(2);
});

test("offered tools are recorded", () => {
  const llm = new FakeLlm([{ type: "text", content: "ok" }]);
  const defs = [{ type: "function", function: { name: "add" } }];
  llm.complete([{ role: "user", content: "x" }], defs);
  expect(llm.turns[0]!.tools).toEqual(defs);
});

test("unplayed turns are visibly unplayed", () => {
  const llm = new FakeLlm([
    { type: "text", content: "a" },
    { type: "text", content: "b" },
  ]);
  llm.complete([{ role: "user", content: "x" }]);
  expect(llm.turns[0]!.messages).not.toBeNull();
  expect(llm.turns[1]!.messages).toBeNull();
});

test("unknown entry type throws", () => {
  const llm = new FakeLlm([{ type: "poem" } as unknown as ScriptEntry]);
  expect(() => llm.complete([])).toThrow("poem");
});

test("script exhaustion throws instead of improvising", () => {
  const llm = new FakeLlm([{ type: "text", content: "only line" }]);
  llm.complete([]);
  expect(() => llm.complete([])).toThrow("exhausted");
});
