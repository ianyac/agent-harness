from dataclasses import dataclass
from pathlib import Path
from typing import cast

from harness.llm import LLMClient
from harness.permissions import PermissionPolicy
from harness.sandbox import Sandbox, SandboxPolicy, default_sandbox
from harness.search import default_provider
from harness.session import SessionLog, lock, unlock
from harness.skills import Skill
from harness.tools.base import Tool
from harness.tools.web import SearchProvider
from server.context_mode import PreparedContext, prepare_context_mode
from server.registry import PlanReviewer, build_registry


COMPACT_FRACTION = 0.8
_DEFAULT_SEARCH = object()


@dataclass(frozen=True)
class RuntimeConfig:
    session_id: str
    workspace: Path
    mode: str
    context_mode: str
    compact_threshold: int | None


@dataclass(frozen=True)
class ToolSafety:
    decision: str
    read_only: bool
    network_egress: bool


@dataclass(frozen=True)
class SafetySnapshot:
    mode: str
    sandbox_backend: str
    workspace_write_boundary: str
    read_breadth: str
    network_policy: str
    tools: dict[str, ToolSafety]


class HarnessRuntime:
    def __init__(
        self,
        config: RuntimeConfig,
        llm: LLMClient,
        session_path: Path,
        *,
        search_provider: SearchProvider | None | object = _DEFAULT_SEARCH,
        resuming: bool | None = None,
    ) -> None:
        self.config = config
        self.workspace = Path(config.workspace).resolve()
        self.session_path = Path(session_path)
        self.llm = llm
        self._closed = False
        self._lock_held = False
        self._plan_reviewer: PlanReviewer | None = None
        self.context: PreparedContext | None = None

        try:
            lock(self.session_path)
            self._lock_held = True
            self.context = prepare_context_mode(
                self.session_path,
                requested=config.context_mode,
                resuming=self.session_path.exists() if resuming is None else resuming,
                compact_threshold=config.compact_threshold,
            )
            if self.context.mode == "compaction" and self.context.compact_threshold is None:
                context_window = getattr(llm, "context_window", None)
                if not isinstance(context_window, int) or context_window <= 0:
                    raise ValueError(
                        "LLM client must report a positive context_window when no compact threshold is configured"
                    )
                self.context.compact_threshold = int(COMPACT_FRACTION * context_window)
            self.policy = PermissionPolicy(config.mode)
            self.sandbox_policy = SandboxPolicy(self.workspace, allow_network=False)
            self.sandbox: Sandbox = default_sandbox(self.sandbox_policy)
            self.session_log = SessionLog(self.session_path)
            self.messages = self.session_log.load()
            active_provider = (
                default_provider()
                if search_provider is _DEFAULT_SEARCH
                else cast(SearchProvider | None, search_provider)
            )
            self.tools, self.skills = build_registry(
                workspace=self.workspace,
                llm=self.llm,
                policy=self.policy,
                sandbox=self.sandbox,
                context=self.context.folding,
                compact_threshold=self.context.compact_threshold,
                plan_reviewer=self.review_plan,
                search_provider=active_provider,
            )
        except BaseException:
            self._close_owned_resources()
            raise

    @property
    def folding(self):
        return self.context.folding if self.context is not None else None

    @property
    def compact_threshold(self) -> int | None:
        return self.context.compact_threshold if self.context is not None else None

    def bind_plan_reviewer(self, reviewer: PlanReviewer) -> None:
        self._plan_reviewer = reviewer

    def review_plan(self, plan: str) -> tuple[bool, str]:
        if self._plan_reviewer is None:
            return False, ""
        return self._plan_reviewer(plan)

    def safety_snapshot(self) -> SafetySnapshot:
        egress = {"web_fetch", "web_search"}
        return SafetySnapshot(
            mode=self.policy.mode,
            sandbox_backend=type(self.sandbox).__name__,
            workspace_write_boundary=str(self.sandbox_policy.workspace),
            read_breadth="host",
            network_policy="allow" if self.sandbox_policy.allow_network else "deny",
            tools={
                name: ToolSafety(
                    decision=self.policy.decide(tool),
                    read_only=tool.read_only,
                    network_egress=name in egress,
                )
                for name, tool in self.tools.items()
            },
        )

    def _close_owned_resources(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
        finally:
            if self._lock_held:
                unlock(self.session_path)
                self._lock_held = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_owned_resources()
