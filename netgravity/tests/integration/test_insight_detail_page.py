"""
The deep dive: what a finding says, how it was reached, and what to do.

The page had the finding's headline, a chart and a table of figures. What it
did not have was the step between the second and the first — a reader could
see 97.2% and see the conclusion, and had to take the move between them on
trust. The one control that promised to explain it, "Why this finding?", was
a disclosure toggle at the foot that revealed the narrative they had already
read at the top.

Four claims are held here.

  * **The prose is the lede, not a caption.** The narrative was printed in an
    `.insd-chart-note` under the canvas, in the slot this file uses for
    provenance footnotes, so the paragraph explaining the finding was typeset
    as a disclaimer about the picture above it.

  * **The reasoning is on the page.** The engine tags every figure with the
    role it played — the measurement, what it was compared against, the driver
    behind it — and those three lists were concatenated into one flat array
    for a table, throwing the distinction away.

  * **The figures are enumerated once.** Rendered against a real solved
    network, a finding citing one figure printed it four times: in the banner,
    in the chain, in the evidence table and again in the metric tiles.

  * **The call to action is this finding's.** The only buttons were a generic
    "Test a change as a scenario" and "Open in Digital Twin", identical on
    every finding — so a capacity risk whose Overview tile said "Open KPIs"
    arrived here offering the scenario planner.

Asset-level, like `test_overview_and_basemap.py`, and for the same reason:
these are defects in bundled JavaScript, and a browser test would report
"the page looks different" without naming which half had drifted.
"""

from __future__ import annotations

import pathlib
import re

from app.backend.app import app


FRONTEND = pathlib.Path(app.root_path).parent / "frontend"


def _asset(*parts: str) -> str:
    path = FRONTEND
    for part in parts:
        path = path / part
    return path.read_text(encoding="utf-8", errors="replace")


def _without_comments(text: str) -> str:
    """Comments here quote the thing being banned; scan the code, not the prose."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    return text


def _fn(js: str, signature: str) -> str:
    """One function body, by its signature."""
    start = js.index(signature)
    return js[start:js.index("\n}\n", start)]


class TestTheFindingIsExplainedBeforeItIsIllustrated:
    def test_the_narrative_leads_the_page_instead_of_captioning_the_chart(self):
        js = _without_comments(_asset("js", "insight-detail.js"))
        fn = _fn(js, "function descriptionHtml(record)")
        assert "insd-lede" in fn, fn
        assert "insightDescription(record, record.title)" in fn, fn

        # And it is placed above the chart, not inside it.
        render = _fn(js, "function renderDeepDive()")
        assert render.index("descriptionHtml(record)") \
            < render.index("findingCardHtml(record, plan)"), render

    def test_the_lede_does_not_repeat_the_headline_it_sits_under(self):
        """
        The engine's headline is usually one of its own narrative's sentences,
        so printing both put the same sentence on the page twice — as the
        title, and again as the first line under it. The deep dive uses the
        Overview tile's rule, from the same module, rather than a second copy
        of it that could drift.
        """
        js = _asset("js", "insight-detail.js")
        assert "from './insight-presentation.js'" in js, js[:2000]
        assert "insightDescription" in js

        shared = _without_comments(_asset("js", "insight-presentation.js"))
        assert "export function insightDescription" in shared

        # And app.js reads the same one rather than keeping its own.
        app_js = _without_comments(_asset("js", "app.js"))
        assert "from './insight-presentation.js'" in app_js
        assert "function insightDescription(" not in app_js, (
            "app.js has grown its own copy of the description rule again")


class TestTheReasoningIsOnThePage:
    def test_the_chain_is_built_from_the_roles_the_engine_tagged(self):
        js = _without_comments(_asset("js", "insight-detail.js"))
        fn = _fn(js, "function reasoningCardHtml(record)")
        for role in ("'metric'", "'comparison'", "'driver'"):
            assert role in fn, (role, fn)
        assert "How this was reached" in fn, fn
        # Ordered, and each figure keeps the engine that computed it.
        assert "insd-reason-chain" in fn, fn
        assert "e.source" in fn, fn

    def test_a_step_with_no_figures_is_dropped_rather_than_shown_empty(self):
        """
        A finding that cites no comparison did not make one. A heading over a
        blank would suggest the engine had weighed something it did not.
        """
        js = _without_comments(_asset("js", "insight-detail.js"))
        fn = _fn(js, "function reasoningCardHtml(record)")
        assert ".filter((step) => step.rows.length)" in fn, fn

    def test_a_finding_with_no_figures_at_all_says_so(self):
        js = _without_comments(_asset("js", "insight-detail.js"))
        fn = _fn(js, "function reasoningCardHtml(record)")
        assert "if (!steps.length)" in fn, fn
        assert "statement about the plan's" in fn, fn

    def test_the_limitation_is_the_analysis_and_not_this_finding(self):
        """
        `limitation` is written across the whole briefing. Attaching it to one
        finding would claim the engine said something about that finding that
        it did not.
        """
        js = _asset("js", "insight-detail.js")
        fn = _fn(_without_comments(js), "function reasoningCardHtml(record)")
        assert "rec.limitation" in fn, fn
        assert "What this analysis does not" in fn, fn

    def test_the_disclosure_toggle_that_repeated_the_lede_is_gone(self):
        """
        "Why this finding?" revealed `record.narrative` — the lede at the top
        of the page. The question it asked is now answered at length in the
        body, where a reader does not have to know to press anything.

        The ACTION view keeps its own "Why is this needed?", which explains
        required versus optional and is not a restatement of anything.
        """
        js = _without_comments(_asset("js", "insight-detail.js"))
        deep = _fn(js, "function renderDeepDive()")
        assert "insd-why-reveal" not in deep, deep
        assert "insd-why-btn" not in deep, deep
        # Still present on the action view, which is a different question.
        assert "insd-why-btn" in _fn(js, "function renderActionDetail()")


class TestTheFiguresAreEnumeratedOnce:
    def test_the_evidence_table_and_the_metric_tiles_are_gone(self):
        """
        Against a real solved network the Cost finding printed
        "Business Network Cost / 150,627.70" four times on one page: the
        banner, the chain, the table and the tiles. Three of those were the
        same list with different furniture round it, and the chain is the one
        that says something the others do not — the role each figure played.
        """
        js = _without_comments(_asset("js", "insight-detail.js"))
        for gone in ("function evidenceCardHtml", "function metricTileHtml",
                     "insd-metrics-row", "insd-table"):
            assert gone not in js, gone

    def test_the_chain_carries_what_those_two_carried(self):
        js = _without_comments(_asset("js", "insight-detail.js"))
        fn = _fn(js, "function reasoningCardHtml(record)")
        # The Role column's content, as step headings.
        assert "What was measured" in fn
        assert "What it was compared against" in fn
        # The "Computed by" column's content, per figure.
        assert "via ${insdEsc(e.source)}" in fn, fn


class TestTheVisualsShowOnlyComparisonsTheEngineMade:
    def test_a_magnitude_bar_needs_two_figures_sharing_a_unit(self):
        """
        A currency total and a percentage drawn on one scale is a picture of
        nothing. And the width is a drawing instruction, not a reading: §9,
        no figure derived from it is printed.
        """
        js = _without_comments(_asset("js", "insight-detail.js"))
        fn = _fn(js, "function reasoningCardHtml(record)")
        assert "group.n < 2" in fn, fn
        assert "e.unit ||" in fn, fn
        # Only `display_value` reaches the reader.
        assert "insd-bar-fill" in fn, fn

    def test_the_scale_does_not_borrow_the_threshold_for_another_theme(self):
        """
        The only threshold this page has is the utilisation one. A cost
        finding that happens to cite a percentage would otherwise be drawn
        against a 90% utilisation line — a comparison nobody made, and one
        every reader would take for one that had been.
        """
        js = _without_comments(_asset("js", "insight-detail.js"))
        fn = _fn(js, "function thresholdScaleHtml(record, plan)")
        assert "['Capacity', 'Utilisation'].includes(record.theme)" in fn, fn

    def test_the_scale_stays_out_of_the_way_of_a_chart_that_has_one(self):
        js = _without_comments(_asset("js", "insight-detail.js"))
        fn = _fn(js, "function thresholdScaleHtml(record, plan)")
        assert "if (plan && plan.threshold != null) return '';" in fn, fn

    def test_the_threshold_is_the_configured_one_and_never_a_literal(self):
        js = _without_comments(_asset("js", "insight-detail.js"))
        fn = _fn(js, "function thresholdScaleHtml(record, plan)")
        assert "utilisationThreshold()" in fn, fn
        assert "90" not in fn, ("a policy constant is written into the scale", fn)

    def test_the_threshold_caption_stands_where_its_threshold_is(self):
        """
        The foot was "0%", "Threshold 90%", "100%" spaced apart, so the
        caption sat at the midpoint of a bar whose mark was at 90%. A label
        naming a position and standing somewhere else teaches a reader that
        positions on this scale do not mean anything.
        """
        js = _asset("js", "insight-detail.js")
        fn = _fn(_without_comments(js), "function thresholdScaleHtml(record, plan)")
        caption = fn[fn.index("insd-scale-caption"):]
        assert "left:${clamp(threshold)}%" in caption[:120], caption[:200]

    def test_a_card_with_neither_a_chart_nor_a_scale_is_omitted(self):
        js = _without_comments(_asset("js", "insight-detail.js"))
        fn = _fn(js, "function findingCardHtml(record, plan)")
        assert "if (!plan && !scale) return '';" in fn, fn


class TestTheCallToActionIsThisFindings:
    def test_the_tile_and_the_page_it_opens_resolve_the_same_destination(self):
        """
        The Overview tile offered "Open KPIs" on a capacity finding and "View
        affected demand" on an unserved-demand one; the page both of them
        opened offered the scenario planner to each. The map lived in app.js,
        which insight-detail.js cannot import — app.js imports IT — so the two
        screens could not have agreed even in principle.
        """
        shared = _without_comments(_asset("js", "insight-presentation.js"))
        assert "export function insightCta(theme, severity)" in shared

        for module in ("app.js", "insight-detail.js"):
            js = _without_comments(_asset("js", module))
            assert "insightCta" in js, module
            assert "const OV_TILE_CTA = {" not in js, (
                f"{module} has grown its own copy of the destination map")

    def test_the_recommended_action_carries_the_button(self):
        js = _without_comments(_asset("js", "insight-detail.js"))
        fn = _fn(js, "function recommendationCardHtml(record, cta)")
        assert "record.recommendedAction" in fn, fn
        assert "insd-rec-cta" in fn, fn
        assert "cta.label" in fn, fn

    def test_the_unserved_demand_route_is_not_a_tab(self):
        """
        `tab: ''` is the one destination that is not a tab: the market-by-
        market breakdown lives in a drawer app.js owns. It is reached through
        the opener that module exposes, because this file cannot import it.
        """
        shared = _without_comments(_asset("js", "insight-presentation.js"))
        cta = _fn(shared, "export function insightCta(theme, severity)")
        assert "View affected demand" in cta, cta
        assert "DEMAND_SHORTFALL.shortMarkets" in cta, cta

        js = _without_comments(_asset("js", "insight-detail.js"))
        bind = _fn(js, "function bindDeepDive(cta)")
        assert "window.openDemandShortfallDetail" in bind, bind

        app_js = _without_comments(_asset("js", "app.js"))
        assert "window.openDemandShortfallDetail = openDemandShortfallDetail" in app_js

    def test_the_secondary_bar_does_not_repeat_the_button_above_it(self):
        """
        Sending a reader to the scenario planner twice, from two buttons with
        different labels, is two answers to one question.
        """
        js = _without_comments(_asset("js", "insight-detail.js"))
        fn = _fn(js, "function actionBarHtml(facilityId, cta)")
        assert "cta.tab !== 'scenarios'" in fn, fn
        assert "cta.tab !== 'twin'" in fn, fn
        assert "if (!buttons.length) return '';" in fn, fn

    def test_nothing_on_this_page_claims_an_action_was_taken(self):
        """
        The prototype offered Approve / Reject buttons that changed their own
        label and nothing else, and an "action taken" state asserting that
        something had happened.
        """
        js = _without_comments(_asset("js", "insight-detail.js"))
        for gone in ("insd-taken-card", "Approve", "Reject"):
            assert gone not in js, gone


class TestThePageUsesTheWidthItHas:
    def test_the_lede_is_not_capped_at_two_thirds_of_the_page(self):
        """
        `max-width: 78ch` resolved to 714px inside a 1320px page, so the
        sentence explaining the finding stopped short of half way across and
        the rest of the line was empty.
        """
        css = _asset("css", "insight-detail.css")
        rule = css[css.index(".insd-lede {"):]
        rule = rule[:rule.index("}")]
        assert "105ch" in rule, rule

    def test_the_page_is_not_letterboxed(self):
        css = _asset("css", "insight-detail.css")
        rule = css[css.index(".insd-page {"):]
        rule = rule[:rule.index("}")]
        assert "1560px" in rule, rule


class TestTheChartSaysWhatItsColoursMean:
    def test_the_bands_are_the_engines_own_thresholds(self):
        """
        Every bar was the same purple: grey if the plan did not use the site,
        red if over the threshold, purple otherwise — so on a healthy network,
        which is most of them, all seven bars were identical and the chart
        carried no more than a list of numbers would.

        The bands are read from `NETWORK_RECOMMENDATION.thresholds`, never a
        90 or a 30 written into this file: a chart inventing its own bands
        would be drawing a judgement nobody made.
        """
        js = _without_comments(_asset("js", "insight-detail.js"))
        fn = _fn(js, "function entityBarStyle(plan)")
        assert "underUtilisationThreshold()" in fn, fn
        assert "plan.threshold" in fn, fn
        for literal in ("90", "30", "85", "95"):
            assert f"= {literal}" not in fn, (literal, fn)

    def test_a_non_percentage_chart_is_coloured_by_what_the_node_is(self):
        js = _without_comments(_asset("js", "insight-detail.js"))
        fn = _fn(js, "function entityBarStyle(plan)")
        assert "NODE_STYLE.plant.color" in fn, fn
        assert "NODE_STYLE.dc.color" in fn, fn

    def test_the_key_lists_only_the_colours_that_are_on_the_chart(self):
        """
        A key with four entries for a chart showing two is a key that has to
        be read twice. It used to say "Solved plan" beside one purple swatch
        whatever the chart was drawn in.
        """
        js = _without_comments(_asset("js", "insight-detail.js"))
        fn = _fn(js, "function findingCardHtml(record, plan)")
        assert "entityBarStyle(plan).legend" in fn, fn
        assert "b.count" in fn, "the key says how many bars carry each colour"

    def test_the_key_sits_under_the_chart_not_in_its_title(self):
        css = _asset("css", "insight-detail.css")
        rule = css[css.index(".insd-chart-legend {"):]
        rule = rule[:rule.index("}")]
        assert "flex-wrap: wrap" in rule, rule
        assert "border-top" in rule, rule


class TestTheDerivationCanLeaveTheApplication:
    def test_the_working_says_how_it_was_done(self):
        """
        The chain showed the steps and the figures and said nothing about
        where either came from — which is what makes a correct derivation
        still read as a black box, and the distinction that matters most is
        whether a language model had a hand in the numbers.
        """
        js = _without_comments(_asset("js", "insight-detail.js"))
        fn = _fn(js, "function reasoningCardHtml(record)")
        assert "insd-method" in fn, fn
        # Matched on substrings that survive the template literal's own line
        # wrapping — the prose is written to a column in the source.
        assert "computed by the optimiser and" in fn, fn
        assert "no model estimates any of" in fn, fn
        assert "checked back against the computed results" in " ".join(
            fn.split()), fn

    def test_there_is_a_download_and_it_names_its_format(self):
        js = _without_comments(_asset("js", "insight-detail.js"))
        fn = _fn(js, "function reasoningCardHtml(record)")
        assert "insd-download-doc" in fn, fn
        assert "DOCX" in fn, "nobody should press a button to find out what they get"

    def test_it_goes_through_the_same_credentials_as_every_other_call(self):
        """
        Deliberately not `window.open(url)`, which is the short way and the
        wrong one: it cannot send a bearer token, it loses the error body on a
        4xx (a blank tab instead of a reason), and a popup blocker eats it.
        """
        client = _without_comments(_asset("js", "integration", "api-client.js"))
        assert "async download(endpoint, params = {})" in client
        assert "credentials: 'include'" in client
        assert "content-disposition" in client, (
            "the file is named by whoever built it, not by the URL")

        service = _without_comments(
            _asset("js", "integration", "services", "insight-service.js"))
        assert "downloadDerivation" in service
        assert "/document" in service

    def test_a_failed_download_says_so_on_the_button(self):
        """
        This is a thing the reader just asked for, and silence is the worst
        answer to a click.
        """
        js = _without_comments(_asset("js", "insight-detail.js"))
        fn = _fn(js, "async function downloadDerivation(button)")
        assert "Could not build the document" in fn, fn
        assert "Preparing" in fn, "a slow button that does not change is pressed twice"
        assert "revokeObjectURL" in fn, "the blob is held in memory until it is"
