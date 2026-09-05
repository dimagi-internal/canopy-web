"""The gog OAuth client is the AGENT's to declare, not this script's to remember.

THE INCIDENT. A deliberate from-scratch cloud rebuild on 2026-09-05 came up with
ace and echo unable to send or read email. `bootstrap_agents.sh` carried a
hardcoded table:

    declare -A GOG_CLIENT=( [ace]=ace [ada]=canopy [echo]=echo [eva]=canopy [hal]=canopy )

whose own comment says it was "verified against a live ~/.config/gogcli
config.json" — a transcription of one machine at one moment. Every one of the five
agents declares `"gog_client": "canopy"` in its own `config/agent.json`, which
ACE's CLAUDE.md names as "the SINGLE source". The table disagreed for exactly two
of them, so their gmail tokens were imported under a client nothing reads, their
OAuth client credential was fetched from the wrong 1Password vault
(`Agent-Ace` instead of `Canopy-Shared`), and ace's readiness drill came back
"HEALTHY for /ace:run but BROKEN for /ace:turn".

It had gone unnoticed because the previous box was hand-repaired after ITS
bootstrap — the same shape as the tarball break `test_install_gog.py` records: a
latent defect that only fires on a rebuild, and that a rebuild therefore exists
to find.

Ordering is load-bearing. Step 2 writes the account->client map BEFORE step 3
clones anything, so on a genuinely fresh box there is no `config/agent.json` to
read yet. The resolution therefore has to happen again per agent, after its clone
lands — which is what `bootstrap_one_agent` does and what the last test here pins.
"""
from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "bootstrap_agents.sh"

# The script uses `declare -A`, which macOS's system bash 3.2 does not have at all.
# The EC2 box and CI both run bash 5, so these run for real there; skipping is the
# honest answer on a dev mac rather than silently testing a rewritten script.
_HAS_ASSOC = subprocess.run(
    ["bash", "-c", "declare -A _t=( [k]=v ) 2>/dev/null && echo yes"],
    capture_output=True, text=True,
).stdout.strip() == "yes"
pytestmark = pytest.mark.skipif(
    not _HAS_ASSOC, reason="bash lacks associative arrays (macOS ships 3.2; the box and CI run 5)"
)


def _agent_repo(root: pathlib.Path, slug: str, gog_client: str | None) -> None:
    d = root / slug / "config"
    d.mkdir(parents=True)
    body: dict = {"email": f"{slug}@dimagi-ai.com"}
    if gog_client is not None:
        body["gog_client"] = gog_client
    (d / "agent.json").write_text(json.dumps(body))


def _extract_function(path: pathlib.Path, name: str) -> str:
    """The literal text of one top-level bash function — same approach as
    test_install_gog.py, because the script ends in an unconditional `main "$@"`
    and sourcing it would run the whole bootstrap."""
    lines = path.read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1])


def _resolve(root: pathlib.Path, slug: str) -> str:
    """Run the real `gog_client_for` against a fake agent root."""
    decl = next(l for l in SCRIPT.read_text().splitlines()
                if l.startswith("declare -A GOG_CLIENT="))
    fn = _extract_function(SCRIPT, "gog_client_for")
    res = subprocess.run(
        ["bash", "-c", f'set -euo pipefail\nAGENT_ROOT="{root}"\n{decl}\n{fn}\ngog_client_for {slug}'],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, f"{res.stdout}\n{res.stderr}"
    return res.stdout.strip()


def test_the_agents_own_declaration_wins_over_the_hardcoded_table(tmp_path):
    """ace declares canopy; the table said `ace`. The declaration is the source."""
    _agent_repo(tmp_path, "ace", "canopy")
    assert _resolve(tmp_path, "ace") == "canopy"


def test_every_fleet_agent_resolves_to_canopy_when_it_says_so(tmp_path):
    for slug in ("ace", "ada", "echo", "eva", "hal"):
        _agent_repo(tmp_path, slug, "canopy")
    for slug in ("ace", "ada", "echo", "eva", "hal"):
        assert _resolve(tmp_path, slug) == "canopy", f"{slug} did not honour its own config"


def test_a_genuinely_dedicated_client_is_still_honoured(tmp_path):
    """The fix is 'read the declaration', not 'hardcode canopy instead' — an agent
    that really does keep its own client must still get it."""
    _agent_repo(tmp_path, "echo", "echo")
    assert _resolve(tmp_path, "echo") == "echo"


@pytest.mark.parametrize("declared", [None, ""])
def test_a_missing_or_empty_declaration_falls_back_to_the_table(tmp_path, declared):
    """Step 2 runs BEFORE any clone exists, so 'no config/agent.json yet' is a
    normal first-boot state, not an error. It must not resolve to empty — an empty
    client silently sends gog at the wrong app, which is the failure this whole
    module is about."""
    _agent_repo(tmp_path, "eva", declared)
    assert _resolve(tmp_path, "eva") == "canopy"


def test_an_unclonable_agent_directory_falls_back_rather_than_failing(tmp_path):
    """No repo at all — the first-boot case for every agent."""
    assert _resolve(tmp_path, "hal") == "canopy"
    assert _resolve(tmp_path, "ace") == "ace"  # the table's value, until the clone lands


def test_bootstrap_rewrites_the_map_after_the_clone_lands():
    """The ordering guarantee. Step 2 can only ever write the fallback on a fresh
    box, so bootstrap_one_agent must upsert the authoritative value once the
    agent's own config is on disk — otherwise config.json keeps saying `ace` while
    the token is imported under `canopy`, and they disagree forever."""
    text = SCRIPT.read_text()
    assert "upsert_account_client" in text, (
        "no per-agent upsert: the account->client map is written once, before any "
        "clone exists, so it can never see an agent's own declaration"
    )
    body = text[text.index("bootstrap_one_agent() {"):]
    body = body[: body.index("\n}\n")]
    assert "upsert_account_client" in body, (
        "bootstrap_one_agent does not re-assert the client after cloning"
    )
