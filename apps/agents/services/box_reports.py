from __future__ import annotations

from apps.harness.models import Runner

from ..models import AgentBootstrapReport


def record_bootstrap_report(agent, *, runner_name, client_creds_ok, mailbox_ok,
                            gog_client="", detail="", turn_client="",
                            turn_ready=None, env_ok=None):
    """Upsert one box's view of this agent. Latest wins — this is current state,
    not a log: the question it answers is "can this agent run RIGHT NOW", and a
    history of that would bury the answer under every prior boot."""

    fields = {
        "client_creds_ok": bool(client_creds_ok),
        "mailbox_ok": bool(mailbox_ok),
        "gog_client": (gog_client or "").strip(),
        "turn_client": (turn_client or "").strip(),
        "detail": (detail or "").strip()[:2000],
    }
    # The tri-states are written ONLY when the box actually looked. "I did not
    # check" is not an observation and must not overwrite one — the same rule
    # the runner's `projects` follows, for the same reason.
    #
    # They were written as None instead, which erased the last real answer: the
    # updater's credentials-only pass (every 30 minutes) does not run
    # `canopy cred sync`, so minutes after a full bootstrap recorded `env_ok` for all
    # five agents, every one of them read "not checked" (labs, 2026-09-24).
    if turn_ready is not None:
        fields["turn_ready"] = bool(turn_ready)
    elif not mailbox_ok:
        # Except that a stored `turn_ready` TRUE cannot outlive a pass that just found no
        # working mailbox: turns read mail through that mailbox, so "turns can read it" is
        # no longer known. A box with no token computes no turn_ready at all, which left
        # lex's row saying true for a day while its mailbox was down and the setup
        # checklist read "done" off it (2026-10-20). A stored false stays: it names why.
        AgentBootstrapReport.objects.filter(
            agent=agent, runner_name=runner_name.strip(), turn_ready=True,
        ).update(turn_ready=None)
    if env_ok is not None:
        fields["env_ok"] = bool(env_ok)

    row, _ = AgentBootstrapReport.objects.update_or_create(
        agent=agent, runner_name=runner_name.strip(), defaults=fields,
    )
    return row


def bootstrap_reports(agent) -> list:
    """Every LIVE box's view of this agent, newest first.

    A retired runner's last report is left out. Rows key on the box's NAME (see
    `AgentBootstrapReport.runner_name`), so retiring a runner never touched them,
    and a box retired on 2026-10-04 (cloud-ec2-test) went on answering "can this
    agent run" for echo/eva/ada — a frozen all-green row from a machine that no
    longer exists, read as a second healthy box by anyone counting rows. A row is
    hidden only when EVERY runner of that name is retired: a report from a name
    with no runner row at all (a laptop that reports before it pairs) still shows,
    and unretiring the runner brings its row back, because nothing is deleted.
    """

    retired = set(
        Runner.objects.filter(status=Runner.RETIRED).values_list("name", flat=True)
    ) - set(
        Runner.objects.exclude(status=Runner.RETIRED).values_list("name", flat=True)
    )
    return list(
        AgentBootstrapReport.objects.filter(agent=agent)
        .exclude(runner_name__in=retired)
        .order_by("-reported_at")
    )
