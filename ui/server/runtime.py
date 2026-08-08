import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from harness.llm import LLMClient
from harness.permissions import PermissionPolicy
from harness.sandbox import NoSandbox, Sandbox, SandboxPolicy, default_sandbox
from harness.search import default_provider
from harness.session import SessionLog, lock, unlock
from harness.skills import Skill
from harness.tools.base import Tool
from harness.tools.web import SearchProvider
from server.context_mode import (
    CONTEXT_MODES,
    PreparedContext,
    prepare_context_mode,
    resolve_context_mode,
)
from server.registry import PlanReviewer, build_registry


COMPACT_FRACTION = 0.8
_DEFAULT_SEARCH = object()
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class RuntimeConfig:
    session_id: str
    workspace: Path
    mode: str
    context_mode: str
    compact_threshold: int | None

    def __post_init__(self) -> None:
        if not _SESSION_ID.fullmatch(self.session_id):
            raise ValueError(f"invalid session_id: {self.session_id!r}")
        object.__setattr__(self, "workspace", Path(self.workspace).resolve())
        PermissionPolicy(self.mode)
        if self.context_mode not in CONTEXT_MODES:
            raise ValueError(
                f"invalid requested context mode: {self.context_mode!r}"
            )
        if self.compact_threshold is not None and self.compact_threshold <= 0:
            raise ValueError("compact threshold must be a positive token count")


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
        self._lock_held = False
        self._context_owned = False
        self._plan_reviewer: PlanReviewer | None = None
        self.context: PreparedContext | None = None

        if not _SESSION_ID.fullmatch(config.session_id):
            raise ValueError(f"invalid session_id: {config.session_id!r}")
        if self.session_path.stem != config.session_id:
            raise ValueError(
                f"session_id {config.session_id!r} does not match session path "
                f"stem {self.session_path.stem!r}"
            )
        if not self.workspace.is_dir():
            raise ValueError(f"workspace is not a directory: {self.workspace}")
        self.policy = PermissionPolicy(config.mode)

        try:
            lock(self.session_path)
            self._lock_held = True
            is_resume = self.session_path.exists() if resuming is None else resuming
            selected_mode = resolve_context_mode(
                self.session_path,
                requested=config.context_mode,
                resuming=is_resume,
                compact_threshold=config.compact_threshold,
            )
            threshold = config.compact_threshold
            if selected_mode == "compaction" and threshold is None:
                context_window = getattr(llm, "context_window", None)
                if not isinstance(context_window, int) or context_window <= 0:
                    raise ValueError(
                        "LLM client must report a positive context_window when no compact threshold is configured"
                    )
                threshold = int(COMPACT_FRACTION * context_window)
            self.context = prepare_context_mode(
                self.session_path,
                requested=config.context_mode,
                resuming=is_resume,
                compact_threshold=threshold,
            )
            self._context_owned = True
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
        except BaseException as error:
            cleanup_errors = self._cleanup_owned_resources(
                rollback_context=True,
                continue_after_error=True,
            )
            if cleanup_errors:
                error.add_note(
                    "cleanup incomplete: "
                    + "; ".join(
                        f"{type(item).__name__}: {item}" for item in cleanup_errors
                    )
                )
                error.cleanup_errors = tuple(  # type: ignore[attr-defined]
                    cleanup_errors
                )
                error.cleanup_state = {  # type: ignore[attr-defined]
                    "context_owned": self._context_owned,
                    "lock_held": self._lock_held,
                }
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
        unenforced = isinstance(self.sandbox, NoSandbox)
        return SafetySnapshot(
            mode=self.policy.mode,
            sandbox_backend=type(self.sandbox).__name__,
            workspace_write_boundary=(
                "unenforced" if unenforced else str(self.sandbox_policy.workspace)
            ),
            read_breadth="unrestricted" if unenforced else "host",
            network_policy=(
                "allow"
                if unenforced or self.sandbox_policy.allow_network
                else "deny"
            ),
            tools={
                name: ToolSafety(
                    decision=self.policy.decide(tool),
                    read_only=tool.read_only,
                    network_egress=name in egress,
                )
                for name, tool in self.tools.items()
            },
        )

    def _cleanup_owned_resources(
        self,
        *,
        rollback_context: bool,
        continue_after_error: bool,
    ) -> list[BaseException]:
        errors: list[BaseException] = []
        if self._context_owned and self.context is not None:
            try:
                if rollback_context:
                    self.context.rollback()
                else:
                    self.context.close()
            except BaseException as error:
                errors.append(error)
            else:
                self._context_owned = False
        if errors and not continue_after_error:
            return errors
        try:
            if self._lock_held:
                unlock(self.session_path)
                self._lock_held = False
        except BaseException as error:
            errors.append(error)
        return errors

    def close(self) -> None:
        errors = self._cleanup_owned_resources(
            rollback_context=False,
            continue_after_error=False,
        )
        if errors:
            raise errors[0]
