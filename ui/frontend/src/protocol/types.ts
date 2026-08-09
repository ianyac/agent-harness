export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };

export type TurnMode = "base" | "plan";
export type BaseMode = "default" | "acceptAll" | "readOnly";
export type PermissionDecision = "yes" | "no" | "always";
export type SubmissionId = string;

export type UserMessage = {
  type: "send_message";
  text: string;
  mode: TurnMode;
  submission_id?: SubmissionId;
};

export type SendMessage = UserMessage;

export type QueuedMessage = {
  type: "queue_message";
  text: string;
  mode: TurnMode;
  submission_id?: SubmissionId | null;
};

export type CancelTurn = {
  type: "cancel_turn";
  turn_id: string;
};

export type PermissionAnswer = {
  type: "answer_permission";
  request_id: string;
  answer: PermissionDecision;
};

export type PlanAnswer = {
  type: "answer_plan";
  request_id: string;
  approved: boolean;
  feedback?: string;
};

export type SetSessionMode = {
  type: "set_session_mode";
  mode: BaseMode;
};

export type ClearQueuedMessage = {
  type: "clear_queued_message";
};

export type ClientEvent =
  | UserMessage
  | QueuedMessage
  | CancelTurn
  | PermissionAnswer
  | PlanAnswer
  | SetSessionMode
  | ClearQueuedMessage;

export type EventEnvelope = {
  session_id: string;
  generation: number;
  sequence: number;
  turn_id: string | null;
};

export type HarnessMessage = JsonObject;
export type SafetySnapshot = JsonObject;

export type SessionSnapshot = EventEnvelope & {
  type: "session_snapshot";
  messages: HarnessMessage[];
  running: boolean;
  queued_message: QueuedMessage | null;
  safety: SafetySnapshot;
};

export type TurnStarted = EventEnvelope & {
  type: "turn_started";
  turn_id: string;
  mode: TurnMode;
  submission_id?: SubmissionId | null;
};

export type AssistantDelta = EventEnvelope & {
  type: "assistant_delta";
  turn_id: string;
  text: string;
};

export type StreamReset = EventEnvelope & {
  type: "stream_reset";
  turn_id: string;
};

export type ActivityStarted = EventEnvelope & {
  type: "activity_started";
  turn_id: string;
  activity_id: string;
  parent_activity_id: string | null;
  actor: string;
  name: string;
  args: JsonObject;
  started_at: string;
};

export type ActivityCompleted = EventEnvelope & {
  type: "activity_completed";
  turn_id: string;
  activity_id: string;
  parent_activity_id: string | null;
  actor: string;
  name: string;
  args: JsonObject;
  result: JsonValue;
  is_error: boolean;
  started_at: string;
  duration_ms: number;
};

export type PermissionRequested = EventEnvelope & {
  type: "permission_requested";
  turn_id: string;
  request_id: string;
  action: string;
  scope: string;
  reason: string;
};

export type PermissionResolved = EventEnvelope & {
  type: "permission_resolved";
  turn_id: string;
  request_id: string;
  answer: PermissionDecision;
};

export type PlanApprovalRequested = EventEnvelope & {
  type: "plan_approval_requested";
  turn_id: string;
  request_id: string;
  plan: string;
};

export type PlanApprovalResolved = EventEnvelope & {
  type: "plan_approval_resolved";
  turn_id: string;
  request_id: string;
  approved: boolean;
  feedback: string;
};

export type ContextUpdated = EventEnvelope & {
  type: "context_updated";
  context: JsonObject;
};

export type TurnStopping = EventEnvelope & {
  type: "turn_stopping";
  turn_id: string;
};

export type TurnCompleted = EventEnvelope & {
  type: "turn_completed";
  turn_id: string;
  messages: HarnessMessage[];
  final_text: string;
};

export type TurnCancelled = EventEnvelope & {
  type: "turn_cancelled";
  turn_id: string;
};

export type TurnFailed = EventEnvelope & {
  type: "turn_failed";
  turn_id: string;
  error_category: string;
  message: string;
};

export type SafetyUpdated = EventEnvelope & {
  type: "safety_updated";
  safety: SafetySnapshot;
};

export type ServerEvent =
  | SessionSnapshot
  | TurnStarted
  | AssistantDelta
  | StreamReset
  | ActivityStarted
  | ActivityCompleted
  | PermissionRequested
  | PermissionResolved
  | PlanApprovalRequested
  | PlanApprovalResolved
  | ContextUpdated
  | TurnStopping
  | TurnCompleted
  | TurnCancelled
  | TurnFailed
  | SafetyUpdated;

export type ActivityStatus = "running" | "complete" | "error";

export type ActivityItem = {
  activityId: string;
  turnId: string;
  parentActivityId: string | null;
  actor: string;
  name: string;
  args: JsonObject;
  startedAt: string;
  status: ActivityStatus;
  result?: JsonValue;
  isError?: boolean;
  durationMs?: number;
};

export type PermissionRequest = {
  turnId: string;
  requestId: string;
  action: string;
  scope: string;
  reason: string;
};

export type PlanReviewRequest = {
  turnId: string;
  requestId: string;
  plan: string;
};

export type RecoverableError = {
  category: string;
  message: string;
};

export type TranscriptTerminal = {
  kind: "completed" | "cancelled" | "failed";
  generation: number;
  sequence: number;
  turnId: string;
  submissionId: SubmissionId | null;
};

export type TranscriptActiveTurn = {
  generation: number;
  sequence: number;
  turnId: string;
  submissionId: SubmissionId | null;
};

export type TranscriptBoundary =
  | "permission"
  | "plan_review"
  | "activity_error"
  | "subagent_completion"
  | "error"
  | "turn_completion";

export type TranscriptActivityTimelineItem = {
  kind: "activity";
  activityId: string;
};

export type TranscriptAssistantTimelineItem = {
  kind: "assistant";
  text: string;
  messageIndex: number | null;
};

export type TranscriptBoundaryTimelineItem = {
  kind: "boundary";
  boundary: TranscriptBoundary;
};

export type PermissionResolution = {
  answer: PermissionDecision;
};

export type PlanReviewResolution = {
  approved: boolean;
  feedback: string;
};

export type TranscriptPermissionTimelineItem = {
  kind: "permission";
  request: PermissionRequest;
  resolution: PermissionResolution | null;
};

export type TranscriptPlanReviewTimelineItem = {
  kind: "plan_review";
  request: PlanReviewRequest;
  resolution: PlanReviewResolution | null;
};

export type TranscriptContextTimelineItem = {
  kind: "context";
  turnId: string | null;
  sequence: number;
  context: JsonObject;
};

export type TranscriptTimelineItem =
  | TranscriptActivityTimelineItem
  | TranscriptAssistantTimelineItem
  | TranscriptBoundaryTimelineItem
  | TranscriptPermissionTimelineItem
  | TranscriptPlanReviewTimelineItem
  | TranscriptContextTimelineItem;

export type TranscriptState = {
  generation: number;
  lastSequence: number;
  messages: HarnessMessage[];
  streamingText: string;
  activities: Record<string, ActivityItem>;
  activityOrder: string[];
  timeline: TranscriptTimelineItem[];
  permission: PermissionRequest | null;
  planReview: PlanReviewRequest | null;
  running: boolean;
  stopping: boolean;
  queued: QueuedMessage | null;
  safety: SafetySnapshot | null;
  latestContext: JsonObject | null;
  error: RecoverableError | null;
  terminal: TranscriptTerminal | null;
  activeTurn: TranscriptActiveTurn | null;
};
