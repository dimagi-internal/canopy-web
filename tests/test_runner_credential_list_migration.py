"""The data migration that turns a runner's fixed credential slots into its
ordered list (harness 0097). The box tried its logins, then token 1, token 2,
then the API key — so that is the order the list starts in, names and all."""
from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

# needs_migrations: it walks the real migration graph, so it cannot run under CI's
# `--nomigrations` suite and has its own job (ci.yml, `Backend pytest (migrations)`).
pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.needs_migrations]

BEFORE = [("harness", "0096_turn_agent_sha")]
AFTER = [("harness", "0097_runner_credential_list")]


def test_slots_become_list_entries_after_the_logins_in_the_order_the_box_used():
    from apps.common.encryption import decrypt_secret, encrypt_secret

    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    old = executor.loader.project_state(BEFORE).apps
    User = old.get_model("auth", "User")
    Runner = old.get_model("harness", "Runner")
    RunnerCredential = old.get_model("harness", "RunnerCredential")
    RunnerLogin = old.get_model("harness", "RunnerLogin")

    owner = User.objects.create(username="jj", email="jjackson@dimagi.com")
    full = Runner.objects.create(name="cloud-ec2-2", kind="cloud", owner=owner)
    RunnerLogin.objects.create(runner=full, key="default", position=0, account="ace@dimagi-ai.com")
    RunnerCredential.objects.create(
        runner=full, claude_token_enc=encrypt_secret("tok-1"), claude_token_label="ace",
        claude_token_secondary_enc=encrypt_secret("tok-2"), claude_token_secondary_label="jj",
        claude_api_key_enc=encrypt_secret("sk-ant-1"))
    zdr = Runner.objects.create(name="cloud-zdr-1", kind="cloud", owner=owner)
    RunnerCredential.objects.create(runner=zdr, claude_api_key_enc=encrypt_secret("sk-ant-zdr"))

    executor.loader.build_graph()
    executor.migrate(AFTER)
    new = executor.loader.project_state(AFTER).apps
    RunnerLogin = new.get_model("harness", "RunnerLogin")

    def listed(runner):
        return [(e.kind, e.name, decrypt_secret(e.value_enc) if e.value_enc else "", e.position)
                for e in RunnerLogin.objects.filter(runner_id=runner.pk).order_by("position")]

    assert listed(full) == [("login", "", "", 0), ("token", "ace", "tok-1", 1),
                            ("token", "jj", "tok-2", 2), ("api_key", "", "sk-ant-1", 3)]
    assert listed(zdr) == [("api_key", "", "sk-ant-zdr", 0)]
