import pdfplumber

def extract_bill_text(pdf_path: str) -> dict:
    """
    Extracts text and line items from a medical bill PDF.
    Returns a dict with raw text and structured line items.
    """
    extracted = {
        "raw_text": "",
        "pages": []
    }

    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text()
            if text:
                extracted["raw_text"] += text + "\n"
                extracted["pages"].append({
                    "page_number": i + 1,
                    "text": text
                })

    return extracted


# if __name__ == "__main__":
#     # Quick test
#     result = extract_bill_text("sample_bills/sample.pdf")
#     print(result["raw_text"][:500])
    
if __name__ == "__main__":
    result = extract_bill_text(r"C:\Users\Asmita\medbill-audit-agent\sample_bills\sample.pdf")
    print(result["raw_text"][:500])