"""
DRPL Backend - Document Type Template Definitions
Pre-defined template structures for proposals, cost statements, and other document types.
"""

DOCUMENT_TYPE_TEMPLATES = {
    "proposal": {
        "label": "Proposal",
        "default_template_variables": {
            "date": "",
            "ref_number": "",
            "addressee": "",
            "subject": "",
        },
        "content_markdown_template": """# Technical Proposal

## 1. Executive Summary

[Provide a concise overview of the proposal, highlighting key aspects of the offering and why your company is the best fit for this project.]

## 2. Understanding of Requirements

[Demonstrate a thorough understanding of the project scope, objectives, and the client's needs as outlined in the tender documents.]

## 3. Proposed Methodology

### 3.1 Approach
[Describe the overall approach to executing the project.]

### 3.2 Work Plan
[Detail the step-by-step plan for project execution.]

### 3.3 Quality Assurance
[Outline quality control measures and standards to be followed.]

## 4. Team Composition

| Sr. No. | Name | Designation | Qualification | Experience (Years) | Role in Project |
|---------|------|-------------|---------------|-------------------|-----------------|
| 1 | | | | | |
| 2 | | | | | |
| 3 | | | | | |

## 5. Project Timeline

| Phase | Activity | Duration | Start Date | End Date |
|-------|----------|----------|------------|----------|
| 1 | Mobilisation | | | |
| 2 | Execution Phase 1 | | | |
| 3 | Execution Phase 2 | | | |
| 4 | Testing & Commissioning | | | |
| 5 | Handover & Documentation | | | |

## 6. Past Experience

[List relevant past projects demonstrating capability and experience in similar work.]

| Sr. No. | Project Name | Client | Contract Value | Year | Scope of Work |
|---------|-------------|--------|---------------|------|---------------|
| 1 | | | | | |
| 2 | | | | | |

## 7. Terms & Conditions

- **Validity**: This proposal is valid for 90 days from the date of submission.
- **Compliance**: All work will comply with applicable Indian Standards and specifications mentioned in the tender.
- **Safety**: All safety norms and regulations will be strictly followed.
""",
    },
    "cost_statement": {
        "label": "Cost Statement",
        "default_template_variables": {
            "date": "",
            "ref_number": "",
            "addressee": "",
            "subject": "",
        },
        "content_markdown_template": """# Financial Proposal / Cost Statement

## 1. Summary of Costs

| Sr. No. | Description | Amount (INR) |
|---------|-------------|-------------|
| A | Material Costs | |
| B | Labour Costs | |
| C | Equipment & Machinery | |
| D | Transportation & Logistics | |
| E | Overheads & Administrative | |
| F | Profit & Margin | |
| | **Total (excl. GST)** | **0.00** |
| | GST @ 18% | |
| | **Grand Total (incl. GST)** | **0.00** |

## 2. Detailed Breakdown of Costs

### 2.1 Material Costs

| Sr. No. | Item | Specification | Unit | Quantity | Unit Rate (INR) | Total (INR) |
|---------|------|---------------|------|----------|----------------|-------------|
| 1 | | | | | | |
| 2 | | | | | | |
| 3 | | | | | | |

### 2.2 Labour Costs

| Sr. No. | Category | No. of Personnel | Duration (Days) | Rate per Day (INR) | Total (INR) |
|---------|----------|-----------------|-----------------|-------------------|-------------|
| 1 | Skilled | | | | |
| 2 | Semi-skilled | | | | |
| 3 | Unskilled | | | | |
| 4 | Supervisory | | | | |

### 2.3 Equipment & Machinery

| Sr. No. | Equipment | Quantity | Duration | Rate (INR) | Total (INR) |
|---------|-----------|----------|----------|-----------|-------------|
| 1 | | | | | |
| 2 | | | | | |

## 3. Payment Schedule

| Milestone | Description | Percentage | Amount (INR) |
|-----------|-------------|-----------|-------------|
| 1 | Mobilisation Advance | | |
| 2 | Interim Payment 1 | | |
| 3 | Interim Payment 2 | | |
| 4 | Final Payment | | |

## 4. Terms & Validity

- **Price Validity**: Prices quoted are valid for 90 days from the date of submission.
- **Price Basis**: All prices are in Indian Rupees (INR) and inclusive of all taxes unless stated otherwise.
- **Escalation**: Prices are firm and not subject to any escalation during the contract period.
- **Payment Terms**: As per tender conditions.
""",
    },
    "letter": {
        "label": "Letter",
        "default_template_variables": {
            "date": "",
            "ref_number": "",
            "addressee": "",
            "subject": "",
        },
        "content_markdown_template": """Dear Sir/Madam,

[Body of the letter goes here.]

[Provide relevant details, context, and any required information.]

Thanking you,

Yours faithfully,

**For DRPL**
""",
    },
    "certificate": {
        "label": "Certificate",
        "default_template_variables": {
            "date": "",
            "ref_number": "",
            "addressee": "",
            "subject": "",
        },
        "content_markdown_template": """# Certificate

This is to certify that [details of certification].

**Project/Contract Details:**
- Contract No.:
- Project Name:
- Location:
- Period:

**Certified that:**

[Statement of certification - e.g., the work has been completed satisfactorily / the materials meet the required specifications / etc.]

**Authorized Signatory**
""",
    },
}


def get_template(document_type: str) -> dict | None:
    """Get template definition for a document type."""
    return DOCUMENT_TYPE_TEMPLATES.get(document_type)


def get_available_types() -> list[dict]:
    """Get list of available document types with labels."""
    return [
        {"value": key, "label": val["label"]}
        for key, val in DOCUMENT_TYPE_TEMPLATES.items()
    ]
