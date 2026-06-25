"""LangGraph agent that audits medical bills.

Wires the lookup and anomaly-detection tools into a ReAct-style agent backed by Google
Gemini. The agent extracts line items, prices them against the reference dataset, flags
anomalies, and writes a patient-facing summary.

Configuration (via environment / .env):
  - GOOGLE_API_KEY    : required, for the Gemini model.
  - GEMINI_MODEL      : optional, defaults to "gemini-2.5-flash-lite".
  - GEMINI_MAX_RETRIES: optional, defaults to 6 (retries with backoff on rate limits).
"""

import os
from functools import lru_cache

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.prebuilt import create_react_agent

from agent.prompts import REPORT_INSTRUCTIONS, SYSTEM_PROMPT
from agent.tools.anomaly_detector import make_find_anomalies_tool
from agent.tools.cpt_lookup import lookup_bill

load_dotenv()

DEFAULT_MODEL = "gemini-2.5-flash-lite"
DEFAULT_OVERCHARGE_RATIO = 1.5


@lru_cache(maxsize=16)
def build_agent(model: str | None = None, overcharge_ratio: float = DEFAULT_OVERCHARGE_RATIO):
    """Build and cache the compiled LangGraph audit agent.

    `model` overrides the Gemini model name; when None it falls back to the GEMINI_MODEL
    env var, then DEFAULT_MODEL. `overcharge_ratio` sets the anomaly tool's sensitivity
    (multiple of the typical price above which a charge is flagged). Cached per
    (model, ratio) so the UI can switch either without rebuilding unnecessarily.
    """
    if not os.getenv("GOOGLE_API_KEY"):
        raise RuntimeError(
            "GOOGLE_API_KEY is not set. Add it to your environment or a .env file."
        )

    model_name = model or os.getenv("GEMINI_MODEL", DEFAULT_MODEL)
    llm = ChatGoogleGenerativeAI(
        model=model_name,
        temperature=0,
        # Retry transient failures (e.g. 429 RESOURCE_EXHAUSTED rate limits) with
        # exponential backoff before surfacing an error to the caller.
        max_retries=int(os.getenv("GEMINI_MAX_RETRIES", "6")),
    )
    tools = [lookup_bill, make_find_anomalies_tool(overcharge_ratio)]
    return create_react_agent(llm, tools, prompt=SYSTEM_PROMPT)


def audit_bill(
    raw_text: str,
    model: str | None = None,
    overcharge_ratio: float = DEFAULT_OVERCHARGE_RATIO,
) -> str:
    """Run a full audit on a bill's raw text and return the agent's summary.

    The agent decides when to call the lookup and anomaly tools; this just kicks it off
    with the bill text and the reporting instructions. `model` optionally overrides the
    Gemini model name; `overcharge_ratio` sets the flagging sensitivity.
    """
    agent = build_agent(model, overcharge_ratio)
    prompt = (
        "Audit the following medical bill.\n\n"
        f"--- BILL TEXT ---\n{raw_text}\n--- END BILL TEXT ---\n\n"
        f"{REPORT_INSTRUCTIONS}"
    )
    result = agent.invoke({"messages": [HumanMessage(content=prompt)]})
    return result["messages"][-1].content


if __name__ == "__main__":
    from agent.tools.pdf_extractor import extract_bill_text

    text = extract_bill_text("sample_bills/sample.pdf")["raw_text"]
    print(audit_bill(text))
