"""LangGraph agent that audits medical bills.

A ReAct-style agent (backed by Google Gemini) investigates a bill using a toolbox that is
pre-wired to that bill: it can list line items, flag overcharges, check for unbundling,
explain individual codes, and draft a dispute letter. The agent decides which tools to
call and in what order, then writes a patient-facing report.

Configuration (via environment / .env):
  - GOOGLE_API_KEY    : required, for the Gemini model.
  - GEMINI_MODEL      : optional, defaults to "gemini-2.5-flash-lite".
  - GEMINI_MAX_RETRIES: optional, defaults to 6 (retries with backoff on rate limits).
"""

import os
from functools import lru_cache

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.prebuilt import create_react_agent

from agent.prompts import REPORT_INSTRUCTIONS, SYSTEM_PROMPT
from agent.tools.anomaly_detector import detect_anomalies
from agent.tools.cpt_lookup import get_code_detail, lookup_codes, parse_line_items
from agent.tools.dispute_letter import compose_letter
from agent.tools.unbundling import check_unbundling

load_dotenv()

DEFAULT_MODEL = "gemini-2.5-flash-lite"
DEFAULT_OVERCHARGE_RATIO = 1.5
# Allow enough agent<->tool turns for a multi-step investigation (several tools, plus
# follow-up explain_code calls) without runaway loops.
_RECURSION_LIMIT = 30


def _message_text(message) -> str:
    """Extract plain text from a message whose content may be a string or a list of
    content blocks (Gemini returns multi-part content as a list of {'text': ...} dicts)."""
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            p if isinstance(p, str) else p.get("text", "")
            for p in content
        ]
        return "".join(parts)
    return str(content)


@lru_cache(maxsize=8)
def _get_llm(model_name: str, max_retries: int) -> ChatGoogleGenerativeAI:
    """Build and cache the Gemini client (the expensive part). Cached per model."""
    if not os.getenv("GOOGLE_API_KEY"):
        raise RuntimeError(
            "GOOGLE_API_KEY is not set. Add it to your environment or a .env file."
        )
    return ChatGoogleGenerativeAI(
        model=model_name,
        temperature=0,
        # Retry transient failures (e.g. 429 rate limits) with exponential backoff.
        max_retries=max_retries,
    )


def make_audit_tools(raw_text: str, overcharge_ratio: float) -> list:
    """Build the agent's toolbox, pre-wired to one bill.

    The bill is parsed once here; each tool closes over the resulting line items, so the
    agent calls them with no (or tiny) arguments instead of passing data around.
    """
    line_items = lookup_codes(parse_line_items(raw_text))
    codes = [li["code"] for li in line_items]

    # Compact, LLM-friendly view of each line item.
    def _summary(li: dict) -> dict:
        return {
            "code": li["code"],
            "description": li["reference_description"] or li["description"],
            "quantity": li["quantity"],
            "charge": li["charge"],
            "typical_charge": li["typical_charge"],
            "medicare_rate": li["medicare_rate"],
            "known": li["known"],
        }

    @tool
    def get_line_items() -> list[dict]:
        """List the bill's line items with reference pricing (typical and Medicare rates).
        Call this first to see what is being audited."""
        return [_summary(li) for li in line_items]

    @tool
    def find_overcharges() -> dict:
        """Flag overcharges, excess quantities, duplicate charges, and unverifiable codes
        on this bill, with an estimated overcharge per item."""
        return detect_anomalies(line_items, overcharge_ratio)

    @tool
    def check_unbundling_on_bill() -> dict:
        """Check this bill for pairs of codes that should not be billed together
        (e.g. a lab panel billed alongside its individual component tests)."""
        return check_unbundling(codes)

    @tool
    def explain_code(code: str) -> dict:
        """Explain one billing code: its description, category, and reference pricing.
        Use this when a code's purpose or fair price is unclear."""
        return get_code_detail(code)

    @tool
    def draft_dispute_letter(codes_to_dispute: list[str]) -> str:
        """Draft a formal dispute letter for the given list of codes, citing the reason
        each is questionable. Call this after deciding which charges warrant a dispute."""
        anomalies = detect_anomalies(line_items, overcharge_ratio)["findings"]
        unbundled = check_unbundling(codes)["findings"]

        lines: list[str] = []
        total = 0.0
        for code in codes_to_dispute:
            li = next((x for x in line_items if x["code"] == code), None)
            charge = f" (charged ${li['charge']:,.2f})" if li else ""
            reasons = [a["message"] for a in anomalies if a["code"] == code]
            for u in unbundled:
                if code in (u["code_a"], u["code_b"]):
                    other = u["code_b"] if code == u["code_a"] else u["code_a"]
                    reasons.append(f"Possible unbundling with {other}: {u['message']}")
            total += sum(
                a.get("estimated_overcharge", 0) for a in anomalies if a["code"] == code
            )
            reason = " ".join(reasons) or "Charge appears inconsistent with typical pricing."
            lines.append(f"  - Code {code}{charge}: {reason}")

        return compose_letter(lines, total, {})

    return [
        get_line_items,
        find_overcharges,
        check_unbundling_on_bill,
        explain_code,
        draft_dispute_letter,
    ]


def audit_bill(
    raw_text: str,
    model: str | None = None,
    overcharge_ratio: float = DEFAULT_OVERCHARGE_RATIO,
) -> str:
    """Run an agentic audit on a bill's raw text and return the agent's report.

    The agent investigates using the bill-bound toolbox and decides which tools to call.
    `model` optionally overrides the Gemini model; `overcharge_ratio` sets the flagging
    sensitivity.
    """
    llm = _get_llm(
        model or os.getenv("GEMINI_MODEL", DEFAULT_MODEL),
        int(os.getenv("GEMINI_MAX_RETRIES", "6")),
    )
    tools = make_audit_tools(raw_text, overcharge_ratio)
    agent = create_react_agent(llm, tools, prompt=SYSTEM_PROMPT)
    config = {"recursion_limit": _RECURSION_LIMIT}

    result = agent.invoke(
        {"messages": [HumanMessage(content=REPORT_INSTRUCTIONS)]}, config=config
    )

    # After its last tool call (e.g. drafting the letter), the model sometimes ends with
    # an empty turn instead of writing the report. Nudge it once to synthesize — all the
    # investigation is already in context, so this turn just produces text.
    if not _message_text(result["messages"][-1]).strip():
        nudge = HumanMessage(
            content="Now write the complete audit report for the patient, following the "
            "report format, based on your investigation above."
        )
        result = agent.invoke(
            {"messages": result["messages"] + [nudge]}, config=config
        )

    return _message_text(result["messages"][-1])


if __name__ == "__main__":
    from agent.tools.pdf_extractor import extract_bill_text

    text = extract_bill_text("sample_bills/sample.pdf")["raw_text"]
    print(audit_bill(text))
