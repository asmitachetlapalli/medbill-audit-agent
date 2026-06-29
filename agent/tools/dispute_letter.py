"""Shared dispute-letter template.

Used both by the Streamlit UI (deterministic letter from the findings table) and by the
agent's `draft_dispute_letter` tool, so the wording stays consistent. Callers build the
list of disputed-charge lines; this assembles the surrounding letter.
"""


def compose_letter(disputed_lines: list[str], total_overcharge: float, fields: dict) -> str:
    """Assemble a dispute letter.

    `disputed_lines` are pre-formatted bullet strings (one per charge). `fields` supplies
    optional sender/patient/provider details; any missing field is left as a [bracketed]
    placeholder for the patient to fill in.
    """

    def val(key: str, placeholder: str) -> str:
        return fields.get(key) or placeholder

    disputed_block = "\n".join(disputed_lines) if disputed_lines else "  - (none)"
    savings_line = (
        f"Based on typical and Medicare-allowed rates, I estimate potential overcharges "
        f"of approximately ${total_overcharge:,.2f}."
        if total_overcharge > 0
        else "Please provide documentation supporting the charges noted above."
    )

    return f"""{val('sender_name', '[Your Name]')}
{val('sender_contact', '[Your Phone / Email]')}
{val('date', '[Date]')}

Billing Department
{val('provider', '[Provider / Hospital Name]')}

Re: Dispute of charges on account {val('account_number', '[Account Number]')}
    Patient: {val('patient_name', '[Patient Name]')}    Date(s) of service: {val('date_of_service', '[Date of Service]')}

To Whom It May Concern:

I am writing to formally dispute the following charges on the above account, which appear
to be inconsistent with typical and Medicare-allowed rates for these services:

{disputed_block}

{savings_line}

I respectfully request the following:
  1. A fully itemized bill listing every charge, procedure code, and quantity.
  2. Written justification for each disputed charge listed above.
  3. A corrected statement, or an explanation of why the charges are accurate.

Please treat this as a formal request and place a hold on any collection activity for the
disputed amounts until this matter is resolved.

Sincerely,
{val('sender_name', '[Your Name]')}

---
Note: This letter is generated from an automated review using estimated reference pricing.
Flagged items are charges to question with your provider, not proven billing errors.
"""
