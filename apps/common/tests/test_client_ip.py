"""`client_ip`: the ALB appends, so the LAST X-Forwarded-For entry is the client."""
from django.test import RequestFactory, override_settings

from apps.common.client_ip import client_ip, from_scope


def _req(xff=None, remote="10.0.0.9"):
    extra = {"REMOTE_ADDR": remote}
    if xff is not None:
        extra["HTTP_X_FORWARDED_FOR"] = xff
    return RequestFactory().get("/", **extra)


def test_a_spoofed_first_entry_is_ignored():
    # The client wrote 1.2.3.4; the ALB appended what it actually saw.
    assert client_ip(_req("1.2.3.4, 203.0.113.7")) == "203.0.113.7"


def test_the_ordinary_single_entry_is_unchanged():
    assert client_ip(_req("203.0.113.7")) == "203.0.113.7"


def test_no_header_falls_back_to_the_socket():
    assert client_ip(_req()) == "10.0.0.9"
    assert client_ip(_req("  ,  ")) == "10.0.0.9"


@override_settings(CANOPY_TRUSTED_PROXY_HOPS=2)
def test_more_hops_count_from_the_end_and_too_few_entries_are_not_trusted():
    assert client_ip(_req("1.2.3.4, 203.0.113.7, 10.1.1.1")) == "203.0.113.7"
    assert client_ip(_req("203.0.113.7")) == "10.0.0.9"


def test_a_socket_reads_the_same_way():
    scope = {"headers": [(b"x-forwarded-for", b"1.2.3.4, 203.0.113.7")],
             "client": ("10.0.0.9", 5555)}
    assert from_scope(scope) == "203.0.113.7"
    assert from_scope({"headers": [], "client": ("10.0.0.9", 1)}) == "10.0.0.9"

