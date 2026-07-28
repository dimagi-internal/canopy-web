"""Transcript path encoding.

`encode_project_dir` is how BOTH runners find a session's transcript on disk.
Getting it wrong does not raise — it resolves to a directory that does not
exist, so the session silently has no durable record at all.
"""
from canopy_transcript import encode_project_dir




def test_underscores_encode_to_dashes():
    """Claude Code maps '_' to '-' like '/' and '.'. Missing it meant a cwd with
    an underscore resolved to a directory that does not exist — the session
    silently had no transcript, so no durable rows and nothing to reset from.

    Verified against the live fleet: of 286 dirs in ~/.claude/projects none
    contains an underscore, and a session run in `…/packages/canopy_runtime` is
    stored as `…-packages-canopy-runtime`.
    """
    assert encode_project_dir("/Users/j/repo/packages/canopy_runtime") == (
        "-Users-j-repo-packages-canopy-runtime")


def test_encoding_collapses_every_separator_it_knows():
    assert encode_project_dir("/a/b.c/d_e") == "-a-b-c-d-e"
