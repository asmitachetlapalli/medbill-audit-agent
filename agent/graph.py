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
from agent.tools.anomaly_detector import find_anomalies
from agent.tools.cpt_lookup import lookup_bill

load_dotenv()

TOOLS = [lookup_bill, find_anomalies]


@lru_cache(maxsize=1)
def build_agent():
    """Build and cache the compiled LangGraph audit agent."""
    if not os.getenv("GOOGLE_API_KEY"):
        raise RuntimeError(
            "GOOGLE_API_KEY is not set. Add it to your environment or a .env file."
        )

    model = ChatGoogleGenerativeAI(
        model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite"),
        temperature=0,
        # Retry transient failures (e.g. 429 RESOURCE_EXHAUSTED rate limits) with
        # exponential backoff before surfacing an error to the caller.
        max_retries=int(os.getenv("GEMINI_MAX_RETRIES", "6")),
    )
    return create_react_agent(model, TOOLS, prompt=SYSTEM_PROMPT)


def audit_bill(raw_text: str) -> str:
    """Run a full audit on a bill's raw text and return the agent's summary.

    The agent decides when to call the lookup and anomaly tools; this just kicks it off
    with the bill text and the reporting instructions.
    """
    agent = build_agent()
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
