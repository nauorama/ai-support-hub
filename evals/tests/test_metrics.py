"""The deterministic metrics (P4-04): each one on a small hand-made input with a known answer."""

from decimal import Decimal

import pytest

from hub_evals import metrics as m

# --- Wilson intervals ---------------------------------------------------------------------------


def test_wilson_zero_of_twenty_allows_up_to_sixteen_percent() -> None:
    # Eval plan §2: "0 errors in 20 still allows a true error rate of up to 16%".
    low, high = m.wilson(0, 20)
    assert low == 0.0
    assert high == pytest.approx(0.1611, abs=1e-4)


def test_wilson_zero_of_five_allows_up_to_forty_three_percent() -> None:
    # Eval plan §2: "0 wrong answers in 5 auto-replies ... up to about 43%".
    assert m.wilson(0, 5)[1] == pytest.approx(0.4345, abs=1e-4)


def test_wilson_eighteen_of_twenty() -> None:
    low, high = m.wilson(18, 20)
    assert low == pytest.approx(0.6990, abs=1e-4)
    assert high == pytest.approx(0.9721, abs=1e-4)


def test_wilson_all_successes_reaches_one_and_empty_is_everything() -> None:
    assert m.wilson(20, 20)[1] == 1.0
    assert m.wilson(0, 0) == (0.0, 1.0)


@pytest.mark.parametrize(("count", "total"), [(-1, 3), (4, 3), (0, -1)])
def test_wilson_refuses_impossible_counts(count: int, total: int) -> None:
    with pytest.raises(ValueError, match="count"):
        m.wilson(count, total)


def test_rate_value_interval_and_text() -> None:
    rate = m.rate([True, True, False, True])
    assert (rate.count, rate.total, rate.value) == (3, 4, 0.75)
    assert rate.to_json()["ci95"] == [round(v, 4) for v in m.wilson(3, 4)]
    assert str(rate).startswith("3 of 4 (75%; 95% CI ")
    empty = m.rate([])
    assert empty.value is None
    assert empty.to_json()["ci95"] is None
    assert str(empty) == "0 of 0"


# --- percentiles --------------------------------------------------------------------------------


def test_percentile_interpolates_between_ranks() -> None:
    values = [4.0, 1.0, 3.0, 2.0]
    assert m.percentile(values, 50) == 2.5
    assert m.percentile(values, 95) == pytest.approx(3.85)
    assert m.percentile(values, 0) == 1.0
    assert m.percentile(values, 100) == 4.0


def test_percentile_of_one_value_and_of_none() -> None:
    assert m.percentile([7.0], 95) == 7.0
    assert m.percentile([], 50) is None
    with pytest.raises(ValueError, match="q must be"):
        m.percentile([1.0], 101)


def test_latency_percentiles() -> None:
    latency = m.latency([1000, 2000, 3000, 4000, 10000])
    assert latency.count == 5
    assert latency.p50_ms == 3000.0
    assert latency.p95_ms == pytest.approx(8800.0)
    assert m.latency([]).to_json() == {"count": 0, "p50_ms": None, "p95_ms": None}


def test_cost_per_ticket_is_the_mean() -> None:
    assert m.cost_per_ticket([Decimal("0.002"), Decimal("0.004")]) == Decimal("0.003")
    assert m.cost_per_ticket([]) is None


# --- triage -------------------------------------------------------------------------------------

PAIRS = [
    ("limits", "limits"),
    ("limits", "fees_fx"),
    ("fees_fx", "fees_fx"),
    ("card_declined", None),  # failed run
]


def test_triage_accuracy_counts_a_missing_prediction_as_wrong() -> None:
    accuracy = m.triage_accuracy(PAIRS)
    assert (accuracy.count, accuracy.total) == (2, 4)


def test_confusion_matrix() -> None:
    assert m.confusion_matrix(PAIRS) == {
        "card_declined": {"none": 1},
        "fees_fx": {"fees_fx": 1},
        "limits": {"fees_fx": 1, "limits": 1},
    }


def test_flag_recall_counts_only_labelled_tickets() -> None:
    def flags(wants: bool, fraud: bool = False, complaint: bool = False) -> dict[str, bool]:
        return {
            "wants_human": wants,
            "reports_unauthorised_activity": fraud,
            "is_complaint_or_legal": complaint,
        }

    recall = m.flag_recall(
        [
            (flags(True), flags(True)),  # found
            (flags(True), flags(False)),  # missed
            (flags(True), None),  # failed run: nothing found
            (flags(False), flags(True)),  # a false alarm does not count for recall
            (flags(False, fraud=True), flags(False, fraud=True)),
        ]
    )
    assert (recall["wants_human"].count, recall["wants_human"].total) == (1, 3)
    assert (recall["reports_unauthorised_activity"].count,) == (1,)
    assert recall["reports_unauthorised_activity"].total == 1
    assert recall["is_complaint_or_legal"].total == 0


# --- routing ------------------------------------------------------------------------------------


def test_routing_metrics() -> None:
    items = [
        m.RoutedTicket(must_handoff=True, should_escalate=True, route="handoff"),
        m.RoutedTicket(must_handoff=True, should_escalate=True, route="approval"),
        m.RoutedTicket(must_handoff=False, should_escalate=True, route="auto_reply"),
        m.RoutedTicket(must_handoff=False, should_escalate=False, route="approval"),
        m.RoutedTicket(
            must_handoff=False, should_escalate=False, route="auto_reply", wrong_answer=False
        ),
    ]
    routes = m.routing(items)
    assert (routes["hard_handoff_recall"].count, routes["hard_handoff_recall"].total) == (1, 2)
    # approval counts as escalated; the auto-reply does not.
    assert (routes["escalation_recall"].count, routes["escalation_recall"].total) == (2, 3)
    assert (routes["escalation_precision"].count, routes["escalation_precision"].total) == (2, 3)
    assert (routes["automation_share"].count, routes["automation_share"].total) == (2, 5)
    # Wrong: the auto-reply that should have escalated; the judged right one is not.
    assert (routes["wrong_auto_reply"].count, routes["wrong_auto_reply"].total) == (1, 2)


def test_an_auto_reply_with_a_wrong_or_unjudged_answer_is_wrong() -> None:
    routes = m.routing(
        [
            m.RoutedTicket(False, False, "auto_reply", wrong_answer=True),
            m.RoutedTicket(False, False, "auto_reply", wrong_answer=None),
        ]
    )
    assert routes["wrong_auto_reply"].count == 2


def test_routing_of_nothing_is_empty() -> None:
    routes = m.routing([])
    assert all(rate.total == 0 for rate in routes.values())


# --- retrieval ----------------------------------------------------------------------------------


def test_first_gold_rank_matches_articles_and_chunks() -> None:
    ranked = ["fees-overview#1", "withdrawal-times#2", "error-codes#ERR-2003"]
    assert m.first_gold_rank(ranked, ["withdrawal-times"]) == 2
    assert m.first_gold_rank(ranked, ["error-codes#ERR-2003"]) == 3
    # A gold chunk id matches only that chunk, not another chunk of the same document.
    assert m.first_gold_rank(ranked, ["error-codes#ERR-2010"]) is None
    assert m.first_gold_rank(["withdrawal-times"], ["withdrawal-times"]) == 1


def test_hit_at_k_and_reciprocal_rank() -> None:
    ranked = ["a#1", "b#1", "c#1", "d#1", "e#1", "gold#1"]
    assert not m.hit_at_k(ranked, ["gold"], k=5)
    assert m.hit_at_k(ranked, ["gold"], k=6)
    assert m.reciprocal_rank(ranked, ["gold"]) == pytest.approx(1 / 6)
    assert m.reciprocal_rank(ranked, ["missing"]) == 0.0


def test_retrieval_leaves_out_tickets_without_a_search_or_gold() -> None:
    scores = m.retrieval(
        [
            (["x#1", "gold#1"], ["gold"]),  # rank 2
            (["gold#1"], ["gold"]),  # rank 1
            (["x#1"], ["gold"]),  # miss
            (None, ["gold"]),  # row-1 handoff: no KB search
            (["gold#1"], []),  # no gold documents
        ]
    )
    assert (scores.covered, scores.excluded) == (3, 2)
    assert (scores.hit_at_k.count, scores.hit_at_k.total) == (2, 3)
    assert scores.mrr == pytest.approx((0.5 + 1 + 0) / 3)


def test_retrieval_without_any_search_is_not_measured() -> None:
    scores = m.retrieval([(None, ["gold"]), (None, [])])
    assert scores.covered == 0
    assert scores.mrr is None
    assert scores.hit_at_k.value is None


# --- answers ------------------------------------------------------------------------------------


def test_citation_code_problems() -> None:
    body = "Fact one. [1] Fact two. [2] Fact three. [3]"
    # [3] has no citation; one marker was dropped by the run (unknown document).
    assert m.citation_code_problems(body, citations=[1, 2], dropped=[4]) == 2
    assert m.citation_code_problems(body, citations=[1, 2, 3], dropped=[]) == 0


def test_mean() -> None:
    assert m.mean([1.0, 0.5]) == 0.75
    assert m.mean([]) is None


# --- targets and red lines ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "status"),
    [
        (0.90, m.Status.PASS),
        (0.85, m.Status.BELOW_TARGET),
        (0.80, m.Status.BELOW_TARGET),  # the red line itself is reached
        (0.75, m.Status.RED_LINE),
        (None, m.Status.NOT_MEASURED),
    ],
)
def test_check_higher_is_better(value: float | None, status: m.Status) -> None:
    assert m.Check("accuracy", value, 0.9, 0.8, True).status is status


@pytest.mark.parametrize(
    ("value", "status"),
    [(0, m.Status.PASS), (1, m.Status.PASS), (2, m.Status.BELOW_TARGET), (3, m.Status.RED_LINE)],
)
def test_check_lower_is_better(value: float, status: m.Status) -> None:
    # Faithfulness: at most 1 unfaithful answer, red line 2.
    assert m.Check("unfaithful", value, 1, 2, False).status is status


def test_check_fraction_on_the_target_passes_and_reported_rows() -> None:
    assert m.Check("coverage", 17 / 20, 0.85, 0.75, True).status is m.Status.PASS
    assert m.Check("MRR", 0.4, None, None, True).status is m.Status.REPORTED
    assert m.Check("latency p50", 9000, 8000, None, False).status is m.Status.BELOW_TARGET
    row = m.Check("x", 0.123456, 0.5, 0.4, True, judged=True).to_json()
    assert row == {
        "name": "x",
        "value": 0.1235,
        "target": 0.5,
        "red_line": 0.4,
        "higher_is_better": True,
        "status": "red line missed",
        "judged": True,
    }
