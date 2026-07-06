export type Message = { role: string } & Record<string, unknown>

export interface PermissionRequest {
  id: string
  name: string
  args: Record<string, unknown>
}

export type ServerEvent =
  | { type: 'session_snapshot'; messages: Message[]; turn_running: boolean;
      pending_permission: PermissionRequest | null; streamed_text: string }
  | { type: 'turn_started' }
  | { type: 'text_delta'; text: string }
  | { type: 'tool_call'; name: string; args: Record<string, unknown> }
  | { type: 'tool_result'; name: string; result: string }
  | { type: 'permission_request'; id: string; name: string; args: Record<string, unknown> }
  | { type: 'compaction'; summarized: number }
  | { type: 'turn_done'; messages: Message[] }
  | { type: 'turn_cancelled' }
  | { type: 'turn_error'; message: string }

export type ClientMessage =
  | { type: 'user_message'; text: string }
  | { type: 'permission_answer'; id: string; answer: 'yes' | 'no' | 'always' }
  | { type: 'cancel' }
