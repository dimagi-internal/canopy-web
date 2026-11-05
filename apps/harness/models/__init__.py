"""Agent-execution harness: runner registry, turns, and the turn-event ledger.

Framework tier. A Turn is the execution envelope for one unit of agent work;
Runners are executors that dial out (heartbeat + claim); TurnEvents are the
append-only ledger. See docs/superpowers/specs/2026-07-05-agent-execution-
control-plane-design.md.

The models live in the submodules; this package re-exports every name so
`from apps.harness.models import X` keeps working.
"""
from __future__ import annotations

from .. import canopy_release, clis  # noqa: F401  (a test patches `models.canopy_release`)

from .agent_state import AgentStateDocument  # noqa: F401
from .credentials import (  # noqa: F401
    RunnerAccessToken,
    RunnerCredential,
    RunnerCredentialRead,
    RunnerCredentialSet,
    RunnerDeviceKey,
    RunnerLogin,
    RunnerMint,
    SignInRequest,
)
from .fleet import (  # noqa: F401
    INVITATION_TTL,
    PAIRING_APPROVAL_GRACE,
    PAIRING_TTL,
    FleetHold,
    PrivateRunner,
    RunnerFleet,
    RunnerInvitation,
    RunnerPairing,
)
from .runner import (  # noqa: F401
    HEARTBEAT_ONLINE_WINDOW,
    SERVES,
    ProjectOptIn,
    Runner,
    RunnerAdmin,
    RunnerAppPreference,
    RunnerAssignment,
    RunnerDrill,
    RunnerEnrollment,
    RunnerFlag,
    RunnerProjectRequest,
    RunnerSlashCommands,
    WorkspaceRunnerOrder,
    serves_of,
)
from .schedule import (  # noqa: F401
    AgentSchedule,
    _default_notify,
)
from .turn import (  # noqa: F401
    ActAsGrant,
    CallerToken,
    FailureInvestigation,
    PendingMail,
    Turn,
    TurnAgentToken,
    TurnEvent,
    TurnTranscript,
    agent_turns_q,
)
