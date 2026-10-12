"""The runner sends the automatic replies canopy-web could not (canopy-web#1416).

No real mail: gog is a fake that records argv and stdin and answers from a
scripted thread.
"""
from __future__ import annotations

import email
import json
import subprocess

from canopy_runner import admission_notices as an

BOX = "hal@dimagi-ai.com"
VIKRAM = "vkumar@dimagi.com"


def _msg(mid, frm, *, headers=(), labels=("UNREAD",)):
    hs = [{"name": "From", "value": frm}, {"name": "Subject", "value": "Can Hal help?"},
          {"name": "Message-ID", "value": f"<{mid}@mail.example>"}, *headers]
    return {"id": mid, "labelIds": list(labels), "payload": {"headers": hs}}


NOTICE = {"id": 7, "agent": "hal", "agent_name": "Hal", "mailbox": BOX, "thread_id": "t-1",
          "message_id": "m-1", "to": VIKRAM, "subject": "Can Hal help?",
          "body": "Hi — this is an automatic reply. Hal can't act on this email on its own."}


class Gog:
    def __init__(self, messages, *, send_rc=0):
        self.messages, self.send_rc, self.calls = messages, send_rc, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw.get("input")))
        verb = argv[2:4]
        if verb == ["thread", "get"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"messages": self.messages}), "")
        if argv[2] == "send":
            return subprocess.CompletedProcess(argv, self.send_rc, json.dumps({"id": "g-9"}),
                                               "" if self.send_rc == 0 else "403 forbidden")
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    def sent(self):
        return [(argv, stdin) for argv, stdin in self.calls if argv[2] == "send"]


def test_a_reply_is_threaded_marked_automatic_and_the_thread_read():
    gog = Gog([_msg("m-1", f"Vikram <{VIKRAM}>")])
    assert an.send_one(NOTICE, BOX, "canopy", runner=gog)[0] == "sent"
    [(argv, raw)] = gog.sent()
    assert argv[argv.index("--thread-id") + 1] == "t-1" and argv[argv.index("--client") + 1] == "canopy"
    assert "--raw-file" in argv and argv[argv.index("--raw-file") + 1] == "-"
    msg = email.message_from_string(raw)
    assert msg["Auto-Submitted"] == "auto-replied"
    assert msg["In-Reply-To"] == "<m-1@mail.example>" and msg["To"] == VIKRAM
    assert msg["Subject"] == "Re: Can Hal help?" and BOX in msg["From"]
    assert msg.get_payload(decode=True).decode().strip() == NOTICE["body"]  # canopy-web's text, unchanged
    assert any(a[2:4] == ["thread", "modify"] and "--remove=UNREAD" in a for a, _ in gog.calls)


def test_a_machine_written_message_is_never_answered():
    for header in ({"name": "Auto-Submitted", "value": "auto-replied"},
                   {"name": "Precedence", "value": "bulk"},
                   {"name": "List-Id", "value": "<x.list>"}):
        gog = Gog([_msg("m-1", VIKRAM, headers=[header])])
        assert an.send_one(NOTICE, BOX, "canopy", runner=gog)[0] == "skipped"
        assert gog.sent() == []


def test_a_reply_already_on_the_thread_is_not_sent_twice():
    gog = Gog([_msg("m-1", VIKRAM),
               _msg("m-2", f"Hal <{BOX}>", headers=[{"name": "Auto-Submitted", "value": "auto-replied"}])])
    assert an.send_one(NOTICE, BOX, "canopy", runner=gog)[0] == "sent"
    assert gog.sent() == []


def test_the_wrong_mailbox_or_a_missing_message_is_a_failure_to_retry():
    assert an.send_one(NOTICE, "other@dimagi-ai.com", "canopy", runner=Gog([]))[0] == "failed"
    gog = Gog([_msg("m-other", VIKRAM)])
    assert an.send_one(NOTICE, BOX, "canopy", runner=gog)[0] == "failed" and gog.sent() == []
    outcome, detail = an.send_one(NOTICE, BOX, "canopy", runner=Gog([_msg("m-1", VIKRAM)], send_rc=1))
    assert outcome == "failed" and "403" in detail


class Client:
    def __init__(self, notices):
        self.notices, self.leased_for, self.reports = notices, None, []

    def claim_admission_notices(self, runner_id, mailboxes):
        self.leased_for = mailboxes
        return self.notices

    def report_admission_notice(self, runner_id, notice_id, outcome, detail=""):
        self.reports.append((notice_id, outcome, detail))
        return True


def test_deliver_leases_for_the_polled_mailboxes_and_reports_each_outcome():
    client = Client([NOTICE])
    gog = Gog([_msg("m-1", VIKRAM)])
    out = an.deliver(client, "r-1", {"hal": {"account": BOX, "client": "canopy"},
                                     "eva": {"account": "", "client": "canopy"}}, runner=gog)
    assert client.leased_for == {"hal": BOX}  # a box with no readable mailbox asks for nothing
    assert [(i, o) for i, o, _ in out] == [(7, "sent")]
    assert client.reports[0][:2] == (7, "sent")


def test_deliver_asks_for_nothing_when_no_mailbox_is_readable():
    client = Client([NOTICE])
    assert an.deliver(client, "r-1", {}) == [] and client.leased_for is None
