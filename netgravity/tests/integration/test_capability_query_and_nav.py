"""
"What can you do?", and the sidebar group that says which figures you are on.

THE CAPABILITY QUESTION
-----------------------
Measured against the running system before `CAPABILITY_QUERY` existed:

    "What can you do?"           -> intent UNKNOWN, confidence 0, 4.4s.
                                    The reply opened "I could not work out what
                                    you would like me to do" and then listed
                                    every distribution centre in the network.
    "What questions can I ask?"  -> intent EXPLANATION, 24.1s. A question about
                                    the assistant ran a workflow over the
                                    network to answer itself.

After: both resolve to CAPABILITY_QUERY in single-digit milliseconds with no
execution id, and the answer is built from the planner's own catalogue.

THE BASELINE GROUP
------------------
The twin and the KPIs both describe the network as uploaded and solved. They
sat as peers beside Forecast and Scenarios, which are different questions
entirely. The group says which figures a reader is looking at; it is a
disclosure and never a destination, because there is no baseline screen.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from app.backend.app import app
from netgravity.orchestrator.conversation.nlu import ConversationalNLU
from netgravity.orchestrator.schemas.requests import Intent


FRONTEND = pathlib.Path(app.root_path).parent / "frontend"


def _asset(*parts: str) -> str:
    path = FRONTEND
    for part in parts:
        path = path / part
    return path.read_text(encoding="utf-8", errors="replace")


def _without_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    return text


CAPABILITY_PHRASINGS = [
    "What can you do?",
    "What can you do for me?",
    "What are your capabilities?",
    "What questions can I ask you?",
    "What can I ask?",
    "How can you help me?",
    "What kind of questions do you answer?",
    "What are you for?",
    "Help me get started.",
]

#: Questions about the NETWORK that share vocabulary with the above. None may
#: be swallowed by the new rule — it is placed first in the cascade, so this is
#: the check that placing it there cost nothing.
NEAR_MISSES = [
    "What can I do about Dallas being over capacity?",
    "How many facilities do I have?",
    "What is my total network cost?",
    "What happens if I close the Dallas DC?",
    "Why is my demand unserved?",
]


class TestAskingWhatTheAssistantCanDo:
    @pytest.mark.parametrize("text", CAPABILITY_PHRASINGS)
    def test_it_is_recognised(self, text):
        nlu = ConversationalNLU()
        lowered = f" {text.lower()} "
        from netgravity.orchestrator.conversation import nlu as nlu_module
        assert any(w in lowered for w in nlu_module._CAPABILITY_WORDS), text

    @pytest.mark.parametrize("text", NEAR_MISSES)
    def test_a_network_question_is_not_swallowed(self, text):
        from netgravity.orchestrator.conversation import nlu as nlu_module
        lowered = f" {text.lower()} "
        assert not any(w in lowered for w in nlu_module._CAPABILITY_WORDS), text

    def test_the_rule_runs_before_every_other(self):
        """
        "What can you do?" carries no metric, status or forecast vocabulary
        and fell through the whole cascade to UNKNOWN; "what questions can I
        ask you?" contains "ask" and reached EXPLANATION, which solves. The
        rule is only correct at the top.
        """
        src = _without_comments(
            (pathlib.Path(app.root_path).parent.parent / "netgravity"
             / "orchestrator" / "conversation" / "nlu.py").read_text(
                encoding="utf-8", errors="replace"))
        body = src[src.index("def _classify("):]
        first = body.index("_CAPABILITY_WORDS")
        for later in ("_HAZARD_WORDS", "_FORECAST_WORDS", "_STATUS_WORDS",
                      "_METRIC_WORDS", "_EXPLAIN_WORDS"):
            assert first < body.index(later), later

    def test_the_answer_is_the_planners_catalogue_not_a_brochure(self):
        """
        A hand-written capability list goes stale the moment a workflow is
        added or removed, and the direction it goes stale in is always "claims
        more than it does".
        """
        src = (pathlib.Path(app.root_path).parent.parent / "netgravity"
               / "orchestrator" / "conversation" / "chat_service.py").read_text(
                   encoding="utf-8", errors="replace")
        fn = src[src.index("def _capability_response("):]
        fn = fn[:fn.index("\n    def ", 10)]
        assert "self.orchestrator.workflows()" in fn, fn
        # The examples are copy; the LIST is not.
        assert "_INTENT_EXAMPLES" in fn, fn
        code = _without_comments(fn)
        assert "wf_" not in code, "a workflow is named in the chat layer"

    def test_it_reaches_no_engine(self):
        """
        Asking what the assistant is for should not cost a solve. It cost 24
        seconds of one before this existed.
        """
        src = (pathlib.Path(app.root_path).parent.parent / "netgravity"
               / "orchestrator" / "conversation" / "chat_service.py").read_text(
                   encoding="utf-8", errors="replace")
        dispatch = src[src.index("# ---- intents answered WITHOUT the solver"):]
        dispatch = dispatch[:dispatch.index("# ---- TRANSLATE")]
        assert "Intent.CAPABILITY_QUERY" in dispatch, dispatch
        fn = src[src.index("def _capability_response("):]
        fn = fn[:fn.index("\n    def ", 10)]
        # Past the docstring, which says the word "solve" several times in the
        # course of explaining that it does not run one.
        body = fn[fn.index('"""', fn.index('"""') + 3) + 3:]
        for engine in ("run_sync", "_to_orchestrator_request", "orchestrator.run"):
            assert engine not in body, (engine, body)

    def test_an_empty_catalogue_is_not_answered_with_a_promise(self):
        src = (pathlib.Path(app.root_path).parent.parent / "netgravity"
               / "orchestrator" / "conversation" / "chat_service.py").read_text(
                   encoding="utf-8", errors="replace")
        fn = src[src.index("def _capability_response("):]
        fn = fn[:fn.index("\n    def ", 10)]
        assert "I cannot list what I can do" in fn, fn

    def test_the_prompt_is_offered_and_offered_first(self):
        html = _asset("index.html")
        faq = html[html.index('id="chatbot-faq-section"'):]
        first = faq.index('data-action="askChatbotPrompt"')
        arg = faq[first:faq.index("\n", first)]
        assert "What can you do?" in arg, arg


class TestTheBaselineGroup:
    def test_the_twin_and_the_kpis_are_under_it(self):
        html = _asset("index.html")
        group = html[html.index('id="nav-group-baseline"'):]
        group = group[:group.index('data-tab="forecast"')]
        assert ">Baseline<" in group, group[:400]
        assert group.index('data-tab="twin"') < group.index(
            'data-tab="facility-dashboard"'), "the twin is not listed first"
        assert "nav-sub" in group

    def test_the_head_opens_the_view_it_stands_for(self):
        """
        SUPERSEDED: this was `test_the_head_is_a_disclosure_and_never_a_
        destination`, on the reasoning that there is no baseline SCREEN, only
        two views of one idea. In use that reads as a dead menu item — the row
        is styled exactly like the four that navigate, and clicking it did
        nothing but fold the group it was already in. Asked for, and correct:
        clicking Baseline opens the Digital Twin.

        What the old test protected is still asserted, because it is still
        true and still load-bearing: the head carries no `data-tab`. Only
        `data-tab` elements are wired as tabs, so a head with one would mark
        ITSELF active and compete with its own child for the selected row.
        `data-group-tab` is a separate attribute read only by `bindNavGroups`.
        """
        html = _asset("index.html")
        head = html[html.index('id="nav-item-baseline"'):]
        head = head[:head.index(">")]
        assert "data-tab=" not in head, head
        assert 'data-group-tab="twin"' in head, head
        assert 'role="button"' in head and "tabindex" in head, head
        assert "aria-expanded" in head, head
        # Only elements WITH a data-tab are wired as tabs.
        js = _without_comments(_asset("js", "app.js"))
        assert "document.querySelectorAll('.nav-item[data-tab]')" in js
        fn = js[js.index("function bindNavGroups()"):]
        fn = fn[:fn.index("\n}\n")]
        assert "head.dataset.groupTab" in fn, fn
        assert "navigateToTab(tab)" in fn, fn
        # It opens the group as well: a destination inside a folded group
        # would mark a row nobody can see.
        assert "setOpen(true)" in fn, fn

    def test_the_caret_still_collapses_and_does_not_navigate(self):
        """
        The two actions had to separate when the head gained one. A caret
        whose click also reached the head would navigate every time the group
        was folded — so the collapse is its own control, with its own hit
        area, and it stops the click there.
        """
        html = _asset("index.html")
        assert 'class="nav-caret-btn"' in html
        js = _without_comments(_asset("js", "app.js"))
        fn = js[js.index("function bindNavGroups()"):]
        fn = fn[:fn.index("\n}\n")]
        caret = fn[fn.index(".nav-caret-btn"):]
        assert "e.stopPropagation()" in caret[:260], caret[:260]
        css = _asset("css", "style.css")
        rule = css[css.index(".nav-caret-btn {"):]
        rule = rule[:rule.index("}")]
        # 20px, not the 14px glyph: a caret that is hard to hit sends the
        # click to the head behind it, which navigates.
        assert "width: 20px" in rule, rule

    def test_it_opens_itself_when_it_holds_the_active_page(self):
        js = _without_comments(_asset("js", "app.js"))
        assert "function syncNavGroups()" in js
        fn = js[js.index("function syncNavGroups()"):]
        fn = fn[:fn.index("\n}\n")]
        assert ".nav-item.active" in fn, fn
        assert "dataset.open = 'true'" in fn, fn
        # Called on every navigation, not only at boot.
        nav = js[js.index("export function navigateToTab(tab)"):]
        # A CODE anchor: `_without_comments` has already removed the
        # "// 1. Home" marker this used to slice on.
        nav = nav[:nav.index("document.querySelectorAll('.tab-panel')")]
        assert "syncNavGroups()" in nav, nav

    def test_the_collapse_can_actually_close(self):
        """
        `grid-template-rows: 0fr` sizes the FIRST row only. With the two nav
        items as direct children the group closed to the height of the second
        one — measured at 69px of 90px, which reads as a broken animation
        rather than a closed menu. One inner wrapper is the row that collapses.
        """
        html = _asset("index.html")
        assert 'class="nav-group-inner"' in html
        css = _asset("css", "style.css")
        assert ".nav-group-inner { overflow: hidden; min-height: 0; }" in css, (
            "the collapsing row has no overflow/min-height"
        )
        rule = css[css.index('.nav-group[data-open="false"] .nav-group-items'):]
        assert "grid-template-rows: 0fr" in rule[:120], rule[:120]

    def test_a_collapsed_sidebar_flattens_the_group(self):
        """An indent rail against a 28px icon column reads as damage."""
        css = _asset("css", "style.css")
        assert ".sidebar.collapsed .nav-item.nav-sub" in css
        assert ".sidebar.collapsed .nav-caret { display: none; }" in css
