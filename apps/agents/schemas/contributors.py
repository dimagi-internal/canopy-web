from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import Field, field_validator

from apps.common.schemas import StrictModel

from .tasks import AgentTaskLink


#: `_http_url` in the schema: an http(s) URL; `OPTIONAL_HTTP_URL` may also be blank.
HTTP_URL = r"^\s*https?://[^\x00]*$"
OPTIONAL_HTTP_URL = r"^\s*(https?://[^\x00]*)?$"


def _http_url(value: str) -> str:
    value = (value or "").strip()
    if value and not value.startswith(("https://", "http://")):
        raise ValueError("must be an http(s) URL")
    return value


class ContributorQuoteIn(StrictModel):
    """Something the person said, verbatim, and where they said it."""

    quote: str = Field(min_length=1, max_length=600, description="Their words, verbatim. Trim, never rephrase.")
    source_url: str = Field(min_length=1, max_length=500, pattern=HTTP_URL,
                            description="Where they said it: a canopy session, a doc comment, an email thread.")
    said_at: dt.datetime | None = None
    where: str = Field(default="", max_length=120,
                       description="A short label for the link, e.g. 'Supply session, Sep 10'.")

    _url = field_validator("source_url")(_http_url)


class ContributorIdeaIn(StrictModel):
    """One of the project's ideas (a `kind: "idea"` task) the agent read as this
    person's, with their words. The page lists the ideas carrying the person's
    own words (their human-why); this is the agent's reading beside it."""

    task: str = Field(min_length=1, max_length=64, description="The idea task's ext_id on this project (T71).")
    quote: str = Field(default="", max_length=600)
    source_url: str = Field(default="", max_length=500, pattern=OPTIONAL_HTTP_URL)
    said_at: dt.datetime | None = None

    _url = field_validator("source_url")(_http_url)


class ContributorOpenIn(StrictModel):
    """Something they want that no task covers yet."""

    text: str = Field(min_length=1, max_length=300, description="A short plain statement of the want.")
    quote: str = Field(default="", max_length=600)
    source_url: str = Field(default="", max_length=500, pattern=OPTIONAL_HTTP_URL)
    said_at: dt.datetime | None = None

    _url = field_validator("source_url")(_http_url)


class ContributorSourceIn(StrictModel):
    label: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=500, pattern=HTTP_URL)

    _url = field_validator("url")(_http_url)


class ContributorSummaryIn(StrictModel):
    """A contributor page, as the agent that read the person's sessions writes it.

    `goal` is the one place the agent's own words appear — keep it short and
    built from theirs; everything else is their words with a link."""

    goal: str = Field(min_length=1, max_length=800,
                      description="What they are trying to achieve on this project, in a few sentences, "
                                  "built from their own words.")
    quotes: list[ContributorQuoteIn] = Field(min_length=1, max_length=12)
    ideas: list[ContributorIdeaIn] = Field(default_factory=list, max_length=60)
    open: list[ContributorOpenIn] = Field(default_factory=list, max_length=20)
    sources: list[ContributorSourceIn] = Field(default_factory=list, max_length=10,
                                               description="What the agent read to write this.")
    basis: str = Field(default="", max_length=300,
                       description="How much it rests on, e.g. '458 prompts across 22 sessions, Sep 10 to Oct 9'.")


class IdeaWhoOut(StrictModel):
    person_id: int | None = Field(description="Null when canopy does not know the person here.")
    name: str


class IdeaTaskOut(StrictModel):
    ext_id: str
    key: str = ""
    title: str
    status: str


class IdeaOut(StrictModel):
    """An idea: a task with `kind: "idea"` — a person's want about the product,
    in their words, with the work toward it as its subtasks."""

    ext_id: str = Field(description="The idea task's agent alias, else its key.")
    key: str = Field(default="", description="The idea task's key.")
    title: str
    status: str = Field(description="The idea task's status.")
    stage: Literal["proposed", "started", "shipped", "declined"] = Field(
        description="Its status in a contributor's words: suggested → proposed, in progress → "
                    "started, done → shipped (marked done by its owner or its person), declined.")
    quote: str = Field(description="Their own words — the idea's first human-why.")
    who: IdeaWhoOut
    source_url: str = ""
    said_at: dt.datetime | None = None
    people: list[int] = Field(description="Everyone whose words it carries (Person ids).")
    subtasks: list[IdeaTaskOut] = Field(description="The work toward it.")
    created_at: dt.datetime
    updated_at: dt.datetime


class IdeaIn(StrictModel):
    """Your own idea, in your own words. Any member may. To record someone
    else's words, create a task with `kind: "idea"` and `human_why`."""

    quote: str = Field(min_length=1, max_length=2000, description="Your words, verbatim.")
    title: str = Field(default="", max_length=300)


class IdeaCreatedOut(StrictModel):
    idea: IdeaOut
    notified: list[str] = Field(description="Who was told about it, by name.")


class StageCountsOut(StrictModel):
    proposed: int = 0
    started: int = 0
    shipped: int = 0
    declined: int = 0


class ContributorBriefOut(StrictModel):
    id: int
    display_name: str
    role: str = ""
    source: Literal["fact", "turn", "manual", "summary", "review", "backing", "reply", "idea"] = Field(
        description="How they joined (a participant row's source), or `idea`: not a participant, "
                    "but an idea's own evidence (its human why, or the turn that raised it) names them.")
    since: dt.datetime
    has_page: bool
    page_withheld: bool = Field(description="A page exists but the reader (an agent) may not use it.")
    goal: str = ""
    written_at: dt.datetime | None = None
    ideas: StageCountsOut


class ProjectIdeasOut(StrictModel):
    agent: str
    project: str
    name: str
    stages: StageCountsOut
    ideas: list[IdeaOut]
    contributors: list[ContributorBriefOut]


class WaitingLineOut(StrictModel):
    text: str = Field(description="One line.")
    url: str = ""
    waiting_on: str = Field(description="Who it waits on, by name.")


class ProjectTeamOut(StrictModel):
    """A project as a teammate sees it: what it is, where it stands, the ideas,
    the people. None of the agent's process — no task cards or numbers."""

    agent: str
    agent_name: str
    project: str
    name: str
    outcome: str
    status: str
    owner_name: str
    links: list[AgentTaskLink]
    may_edit: bool = Field(description="May the caller edit the project's work (the work board is theirs).")
    my_person_id: int | None = Field(default=None, description="The caller as a Person: they may mark their own ideas shipped.")
    built: list[IdeaOut] = Field(description="Ideas marked shipped.")
    in_progress: list[IdeaOut] = Field(description="Ideas a task has started on.")
    waiting: list[WaitingLineOut] = Field(description="Open decisions waiting on people, one line each.")
    stages: StageCountsOut
    ideas: list[IdeaOut]
    contributors: list[ContributorBriefOut]


class ContributorQuoteOut(StrictModel):
    quote: str
    source_url: str
    said_at: dt.datetime | None = None
    where: str = ""


class ContributorOpenOut(StrictModel):
    text: str
    quote: str = ""
    source_url: str = ""
    said_at: dt.datetime | None = None


class ContributorSummaryOut(StrictModel):
    goal: str
    quotes: list[ContributorQuoteOut]
    open: list[ContributorOpenOut]
    sources: list[AgentTaskLink]
    basis: str = ""
    written_at: dt.datetime | None = None
    written_by: str = ""


class ContributorPersonOut(StrictModel):
    id: int
    display_name: str
    email: str = ""


class ContributorPageOut(StrictModel):
    person: ContributorPersonOut
    agent: str
    project: str
    project_name: str
    role: str = ""
    participant_since: dt.datetime | None = None
    summary_state: Literal["written", "not_written", "withheld"]
    summary: ContributorSummaryOut | None = None
    ideas: list[IdeaOut]
    stages: StageCountsOut
    may_write: bool = Field(description="May the caller write this page (an agent with the person's "
                                        "permission, or the person themself)?")
    why_not: str = ""


# ---- engagement: what people do with projects and huddles ---------------------
