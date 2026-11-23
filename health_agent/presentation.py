"""Serializable result shared by the CLI and web presentation layer."""

from dataclasses import asdict

from .orchestrator import Turn


def turn_details(turn: Turn, *, include_code: bool = True) -> dict:
    sandbox = [asdict(analysis) for analysis in turn.sandbox_analyses]
    if not include_code:
        for analysis in sandbox:
            for attempt in analysis["attempts"]:
                attempt.pop("code")
    return {
        "route": asdict(turn.route),
        "workflow": {
            "main_agent": turn.workflow.main_agent,
            "supporting_agents": turn.workflow.supporting_agents,
            "assignment_source": turn.workflow.assignment_source,
            "subquery_source": turn.workflow.subquery_source,
        },
        "collaboration_plan": [asdict(plan) for plan in turn.collaborations],
        "trace": [asdict(event) for event in turn.trace],
        "reflection": asdict(turn.reflection) if turn.reflection else None,
        "sandbox_analyses": sandbox,
        "tool_calls": [asdict(call) for call in turn.tool_calls],
        "evidence_runs": [asdict(report) for report in turn.evidence_runs],
        "coaching_turns": [asdict(coaching) for coaching in turn.coaching_turns],
        "global_reflections": [asdict(review) for review in turn.global_reflections],
        "skill_events": [asdict(event) for event in turn.skill_events],
    }
