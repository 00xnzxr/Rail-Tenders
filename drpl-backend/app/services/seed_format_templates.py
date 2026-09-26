"""
DRPL Backend - Seed Format Templates
Creates 10 system format templates for common Indian railway/government tender documents.
"""

import logging
from sqlalchemy.orm import Session
from app.models.workspace import DocumentFormatTemplate

logger = logging.getLogger(__name__)

SYSTEM_TEMPLATES = [
    # ── 1. Annexure - General Declaration ────────────────────────────────────
    {
        "name": "Annexure - General Declaration",
        "description": "Standard annexure format for formal declarations, proformas, and general annexure submissions in Indian government tenders.",
        "document_category": "annexure",
        "structure_json": {
            "sections": ["header", "reference", "addressee", "declaration_body", "signatory_block"],
            "layout": "formal_document",
        },
        "content_template_markdown": (
            "# Annexure-[NUMBER]: [TITLE]\n\n"
            "**Ref:** NIT No. [NIT_NUMBER] dated [DATE]\n\n"
            "**To,**\n"
            "[ADDRESSEE_NAME]\n"
            "[ADDRESSEE_DESIGNATION]\n"
            "[ADDRESSEE_ORGANISATION]\n"
            "[ADDRESSEE_ADDRESS]\n\n"
            "**Subject:** [SUBJECT_LINE]\n\n"
            "Dear Sir/Madam,\n\n"
            "We, [COMPANY_NAME], having our registered office at [REGISTERED_ADDRESS], "
            "do hereby solemnly declare and affirm as under:\n\n"
            "1. [DECLARATION_POINT_1]\n"
            "2. [DECLARATION_POINT_2]\n"
            "3. [DECLARATION_POINT_3]\n\n"
            "We further declare that the above statements are true and correct to the best of "
            "our knowledge and belief.\n\n"
            "---\n\n"
            "**For and on behalf of [COMPANY_NAME]**\n\n"
            "Signature: ____________________\n"
            "Name: [SIGNATORY_NAME]\n"
            "Designation: [SIGNATORY_DESIGNATION]\n"
            "Date: [DATE]\n"
            "Place: [PLACE]\n"
            "Company Seal\n"
        ),
        "format_rules": [
            "Must reference NIT/tender number in the header",
            "Use DD/MM/YYYY date format throughout",
            "Include company seal placeholder after signatory block",
            "Use 'For and on behalf of [Company Name]' pattern for authorization",
            "All declarations must be numbered sequentially",
        ],
        "required_sections": ["header", "reference", "addressee", "declaration_body", "signatory_block"],
        "match_patterns": ["annexure*", "*declaration*", "*proforma*", "*annexure *"],
    },
    # ── 2. Undertaking / Affidavit ───────────────────────────────────────────
    {
        "name": "Undertaking / Affidavit",
        "description": "Sworn undertaking or affidavit format on stamp paper for self-declarations, non-blacklisting certificates, and binding commitments.",
        "document_category": "declaration",
        "structure_json": {
            "sections": ["stamp_paper_notice", "deponent_identification", "sworn_statements", "verification", "deponent_signature"],
            "layout": "legal_affidavit",
        },
        "content_template_markdown": (
            "# UNDERTAKING / AFFIDAVIT\n\n"
            "**(On Non-Judicial Stamp Paper of appropriate value)**\n\n"
            "**Ref:** NIT No. [NIT_NUMBER] dated [DATE]\n\n"
            "I/We, [DEPONENT_NAME], [DESIGNATION] of M/s [COMPANY_NAME], "
            "having its registered office at [FULL_REGISTERED_ADDRESS], "
            "do hereby solemnly affirm and declare as under:\n\n"
            "1. That I/we am/are the authorized signatory of the above-mentioned firm/company "
            "and am competent to sign this undertaking.\n\n"
            "2. That our firm/company has not been blacklisted or debarred by any "
            "Government Department / Public Sector Undertaking / Autonomous Body in India.\n\n"
            "3. [ADDITIONAL_UNDERTAKING_POINT]\n\n"
            "4. [ADDITIONAL_UNDERTAKING_POINT]\n\n"
            "5. That the above statements are true and correct to the best of my/our "
            "knowledge and belief.\n\n"
            "---\n\n"
            "**VERIFICATION**\n\n"
            "I/We, the above-named deponent, do hereby verify that the contents of the above "
            "affidavit are true and correct to the best of my/our knowledge and belief, "
            "and nothing material has been concealed therefrom.\n\n"
            "Verified at [PLACE] on this [DAY] day of [MONTH], [YEAR].\n\n"
            "**DEPONENT**\n\n"
            "Signature: ____________________\n"
            "Name: [DEPONENT_NAME]\n"
            "Designation: [DESIGNATION]\n"
            "Company Seal\n\n"
            "**Before me,**\n"
            "Notary Public\n"
        ),
        "format_rules": [
            "Must be on non-judicial stamp paper of appropriate value",
            "Include notary verification clause at the bottom",
            "Use first person language ('I/We hereby declare...')",
            "Include full registered address of the deponent's organization",
            "Verification clause must state place and date explicitly",
        ],
        "required_sections": ["stamp_paper_notice", "deponent_identification", "sworn_statements", "verification", "deponent_signature"],
        "match_patterns": ["*undertaking*", "*affidavit*", "*non-blacklisting*", "*self-declaration*", "*sworn statement*"],
    },
    # ── 3. Bill of Quantities (BOQ) ──────────────────────────────────────────
    {
        "name": "Bill of Quantities (BOQ)",
        "description": "Standard BOQ format with itemized rates, quantities, and amounts for financial/price bid submissions.",
        "document_category": "boq",
        "structure_json": {
            "sections": ["header", "work_details", "item_table", "subtotal", "taxes", "grand_total", "notes", "signatory"],
            "layout": "tabular_financial",
        },
        "content_template_markdown": (
            "# BILL OF QUANTITIES / SCHEDULE OF RATES\n\n"
            "**Name of Work:** [WORK_NAME]\n"
            "**NIT No.:** [NIT_NUMBER]\n"
            "**Name of Tenderer:** [TENDERER_NAME]\n\n"
            "| S.No. | Description of Item | Unit | Quantity | Rate (INR) | Amount (INR) |\n"
            "|-------|-------------------|------|----------|-----------|-------------|\n"
            "| 1     | [ITEM_DESCRIPTION] | [UNIT] | [QTY] | [RATE] | [AMOUNT] |\n"
            "| 2     | [ITEM_DESCRIPTION] | [UNIT] | [QTY] | [RATE] | [AMOUNT] |\n"
            "| 3     | [ITEM_DESCRIPTION] | [UNIT] | [QTY] | [RATE] | [AMOUNT] |\n\n"
            "| | | | | **Sub-Total** | **[SUB_TOTAL]** |\n"
            "| | | | | GST @ [RATE]% | [GST_AMOUNT] |\n"
            "| | | | | **Grand Total** | **[GRAND_TOTAL]** |\n\n"
            "**Grand Total (in words):** Rupees [AMOUNT_IN_WORDS] only.\n\n"
            "**Notes:**\n"
            "1. All rates are inclusive of all taxes except GST unless specified.\n"
            "2. Rates shall be quoted in both figures and words. In case of discrepancy, rates in words shall prevail.\n"
            "3. Any item left unquoted shall be deemed to be included in other items.\n\n"
            "---\n\n"
            "**Signature of Tenderer:** ____________________\n"
            "**Name:** [TENDERER_NAME]\n"
            "**Date:** [DATE]\n"
            "**Company Seal**\n"
        ),
        "format_rules": [
            "All amounts must be in INR with Indian number formatting (lakhs, crores)",
            "Include separate GST/tax row(s) after subtotal",
            "Grand total must be expressed in both figures and words",
            "Rates should follow DSR (Delhi Schedule of Rates) or market rate conventions where applicable",
            "Serial numbers must be sequential without gaps",
            "Unquoted items clause is mandatory in notes",
        ],
        "required_sections": ["header", "work_details", "item_table", "grand_total", "notes", "signatory"],
        "match_patterns": ["*bill of quantities*", "*boq*", "*schedule of rates*", "*price bid*", "*financial bid*", "*rate schedule*"],
    },
    # ── 4. Compliance Certificate / Statement ────────────────────────────────
    {
        "name": "Compliance Certificate / Statement",
        "description": "Clause-by-clause compliance statement showing adherence to tender technical and commercial requirements.",
        "document_category": "certificate",
        "structure_json": {
            "sections": ["header", "bidder_details", "compliance_table", "overall_statement", "signatory"],
            "layout": "tabular_compliance",
        },
        "content_template_markdown": (
            "# COMPLIANCE CERTIFICATE / STATEMENT\n\n"
            "**NIT No.:** [NIT_NUMBER]\n"
            "**Name of Work:** [WORK_NAME]\n"
            "**Name of Bidder:** [BIDDER_NAME]\n\n"
            "We hereby confirm compliance with the tender requirements as detailed below:\n\n"
            "| S.No. | Clause / Requirement | Reference | Complied (Yes/No/Partial) | Remarks / Deviation |\n"
            "|-------|---------------------|-----------|--------------------------|--------------------|\n"
            "| 1 | [CLAUSE_DESCRIPTION] | Cl. [X.X] | Yes | Fully complied |\n"
            "| 2 | [CLAUSE_DESCRIPTION] | Cl. [X.X] | Yes | Fully complied |\n"
            "| 3 | [CLAUSE_DESCRIPTION] | Cl. [X.X] | Partial | [DEVIATION_DETAIL] |\n\n"
            "**Overall Compliance Statement:**\n\n"
            "We hereby declare that we have read and understood all the terms, conditions, "
            "and specifications of the above-mentioned tender. We confirm our full compliance "
            "with all requirements except as specifically noted above.\n\n"
            "All deviations, if any, have been clearly mentioned in the Remarks column. "
            "In the absence of any entry, it shall be treated as full compliance.\n\n"
            "---\n\n"
            "**For and on behalf of [COMPANY_NAME]**\n\n"
            "Signature: ____________________\n"
            "Name: [SIGNATORY_NAME]\n"
            "Designation: [SIGNATORY_DESIGNATION]\n"
            "Date: [DATE]\n"
            "Company Seal\n"
        ),
        "format_rules": [
            "Must address each tender clause/requirement individually in the table",
            "Use only Yes/No/Partial as compliance responses",
            "Partial compliance must include justification and deviation details in Remarks",
            "Reference specific tender clause numbers for each row",
            "Include an overall compliance declaration statement after the table",
        ],
        "required_sections": ["header", "bidder_details", "compliance_table", "overall_statement", "signatory"],
        "match_patterns": ["*compliance*", "*deviation*", "*clause by clause*", "*technical compliance*", "*compliance statement*"],
    },
    # ── 5. Technical Methodology / Work Plan ─────────────────────────────────
    {
        "name": "Technical Methodology / Work Plan",
        "description": "Structured technical proposal with methodology, resource deployment, equipment plan, quality assurance, and implementation schedule.",
        "document_category": "proposal",
        "structure_json": {
            "sections": [
                "executive_summary", "scope_understanding", "proposed_methodology",
                "resource_deployment", "equipment_machinery", "quality_assurance",
                "safety_plan", "implementation_schedule",
            ],
            "layout": "technical_document",
        },
        "content_template_markdown": (
            "# TECHNICAL METHODOLOGY / WORK PLAN\n\n"
            "**NIT No.:** [NIT_NUMBER]\n"
            "**Name of Work:** [WORK_NAME]\n"
            "**Name of Bidder:** [BIDDER_NAME]\n\n"
            "## 1. Executive Summary\n\n"
            "[Brief overview of the approach and key deliverables]\n\n"
            "## 2. Understanding of Scope\n\n"
            "[Demonstrate understanding of the work requirements and site conditions]\n\n"
            "## 3. Proposed Methodology\n\n"
            "### 3.1 Approach\n"
            "[Overall approach to the work]\n\n"
            "### 3.2 Key Activities\n"
            "1. [ACTIVITY_1]\n"
            "2. [ACTIVITY_2]\n"
            "3. [ACTIVITY_3]\n\n"
            "### 3.3 Standards & Specifications\n"
            "[Reference to IS codes, IRC standards, RDSO specifications as applicable]\n\n"
            "## 4. Resource Deployment\n\n"
            "| S.No. | Designation | Qualification | Experience | Number |\n"
            "|-------|------------|---------------|------------|--------|\n"
            "| 1 | Project Manager | B.E./B.Tech Civil | 15+ years | 1 |\n"
            "| 2 | Site Engineer | B.E./B.Tech | 5+ years | [N] |\n\n"
            "## 5. Equipment & Machinery\n\n"
            "| S.No. | Equipment | Capacity | Quantity | Owned/Hired |\n"
            "|-------|-----------|----------|----------|-------------|\n"
            "| 1 | [EQUIPMENT] | [CAPACITY] | [QTY] | Owned |\n\n"
            "## 6. Quality Assurance Plan\n\n"
            "[Quality control measures, testing protocols, and acceptance criteria]\n\n"
            "## 7. Safety & Environment Plan\n\n"
            "[Safety measures, PPE requirements, environmental mitigation]\n\n"
            "## 8. Implementation Schedule\n\n"
            "| S.No. | Activity / Milestone | Start | End | Duration |\n"
            "|-------|---------------------|-------|-----|----------|\n"
            "| 1 | Mobilization | Week 1 | Week 2 | 2 weeks |\n"
            "| 2 | [MILESTONE] | [START] | [END] | [DURATION] |\n\n"
            "---\n\n"
            "**For and on behalf of [COMPANY_NAME]**\n\n"
            "Signature: ____________________\n"
            "Name: [SIGNATORY_NAME]\n"
            "Designation: [SIGNATORY_DESIGNATION]\n"
            "Date: [DATE]\n"
        ),
        "format_rules": [
            "Use numbered sections and sub-sections throughout",
            "Reference IS/IRC/RDSO standards where applicable",
            "Resource deployment table must include qualifications and experience",
            "Equipment table must specify owned vs. hired status",
            "Safety and environment plan section is mandatory",
            "Implementation schedule must include milestones with durations",
        ],
        "required_sections": [
            "executive_summary", "scope_understanding", "proposed_methodology",
            "resource_deployment", "equipment_machinery", "quality_assurance",
            "safety_plan", "implementation_schedule",
        ],
        "match_patterns": ["*methodology*", "*technical proposal*", "*work plan*", "*method statement*", "*technical bid*", "*technical approach*"],
    },
    # ── 6. Experience / Credential Certificate ───────────────────────────────
    {
        "name": "Experience / Credential Certificate",
        "description": "Statement of past experience and similar works completed, with contract values and completion certificate references.",
        "document_category": "certificate",
        "structure_json": {
            "sections": ["header", "experience_table", "notes", "signatory"],
            "layout": "tabular_document",
        },
        "content_template_markdown": (
            "# STATEMENT OF EXPERIENCE / CREDENTIALS\n\n"
            "**NIT No.:** [NIT_NUMBER]\n"
            "**Name of Bidder:** [BIDDER_NAME]\n\n"
            "We hereby furnish below the details of similar works completed during the last "
            "[5/7] years:\n\n"
            "| S.No. | Name of Work | Client / Department | Contract Value (₹ Lakhs) | "
            "Period (From-To) | Status | Completion Certificate Ref. |\n"
            "|-------|-------------|-------------------|------------------------|"
            "----------------|--------|----------------------------|\n"
            "| 1 | [WORK_NAME] | [CLIENT] | [VALUE] | [FROM]-[TO] | Completed | [CERT_REF] |\n"
            "| 2 | [WORK_NAME] | [CLIENT] | [VALUE] | [FROM]-[TO] | Completed | [CERT_REF] |\n"
            "| 3 | [WORK_NAME] | [CLIENT] | [VALUE] | [FROM]-[TO] | In Progress | - |\n\n"
            "**Note:** Copies of completion certificates for completed works are enclosed herewith.\n\n"
            "---\n\n"
            "**For and on behalf of [COMPANY_NAME]**\n\n"
            "Signature: ____________________\n"
            "Name: [SIGNATORY_NAME]\n"
            "Designation: [SIGNATORY_DESIGNATION]\n"
            "Date: [DATE]\n"
            "Company Seal\n"
        ),
        "format_rules": [
            "Include works from the last 5-7 years as specified by the tender",
            "Contract values must be in Lakhs or Crores with Indian number formatting",
            "Include completion certificate reference numbers for completed works",
            "Works must be of similar nature and value as specified in eligibility criteria",
            "Clearly distinguish between completed and ongoing works",
        ],
        "required_sections": ["header", "experience_table", "notes", "signatory"],
        "match_patterns": ["*experience*", "*credential*", "*similar work*", "*past performance*", "*track record*", "*work experience*"],
    },
    # ── 7. Covering / Bid Letter ─────────────────────────────────────────────
    {
        "name": "Covering / Bid Letter",
        "description": "Formal covering letter or forwarding letter accompanying the tender submission, with work details and confirmations.",
        "document_category": "letter",
        "structure_json": {
            "sections": ["letterhead", "reference_date", "addressee", "subject", "work_details", "confirmations", "closing", "signatory"],
            "layout": "formal_letter",
        },
        "content_template_markdown": (
            "# [COMPANY_NAME]\n"
            "*[REGISTERED_ADDRESS]*\n"
            "*GSTIN: [GSTIN] | PAN: [PAN] | CIN: [CIN]*\n\n"
            "**Ref No.:** [COMPANY_REF]/[YEAR]\n"
            "**Date:** [DATE]\n\n"
            "**To,**\n"
            "[ADDRESSEE_NAME]\n"
            "[ADDRESSEE_DESIGNATION]\n"
            "[ADDRESSEE_ORGANISATION]\n"
            "[ADDRESSEE_ADDRESS]\n\n"
            "**Subject:** Submission of Tender for [WORK_NAME] — NIT No. [NIT_NUMBER]\n\n"
            "Dear Sir/Madam,\n\n"
            "With reference to the above-mentioned NIT, we hereby submit our tender for "
            "the following work:\n\n"
            "| Particular | Details |\n"
            "|-----------|--------|\n"
            "| Name of Work | [WORK_NAME] |\n"
            "| Estimated Cost | ₹[ESTIMATED_COST] |\n"
            "| EMD Amount | ₹[EMD_AMOUNT] |\n"
            "| Bid Validity | [VALIDITY_PERIOD] days |\n\n"
            "We hereby confirm that:\n\n"
            "1. We have carefully read and understood all the terms and conditions of the tender "
            "document and agree to abide by them unconditionally.\n"
            "2. The information furnished in the bid is true and correct to the best of our knowledge.\n"
            "3. We have not been blacklisted or debarred by any Government Department / PSU.\n"
            "4. Our bid shall remain valid for a period of [VALIDITY_PERIOD] days from the date "
            "of opening of the technical bid.\n"
            "5. [ADDITIONAL_CONFIRMATION]\n\n"
            "Thanking you,\n\n"
            "Yours faithfully,\n\n"
            "**For and on behalf of [COMPANY_NAME]**\n\n"
            "Signature: ____________________\n"
            "Name: [SIGNATORY_NAME]\n"
            "Designation: [SIGNATORY_DESIGNATION]\n"
            "Date: [DATE]\n"
            "Company Seal\n"
        ),
        "format_rules": [
            "Must include company letterhead details (GSTIN, PAN, CIN/Registration)",
            "Reference NIT number and date in the subject line",
            "Include EMD amount and bid validity period in work details table",
            "Unconditional acceptance of terms must be explicitly stated",
            "Use formal salutation and closing ('Dear Sir/Madam', 'Yours faithfully')",
        ],
        "required_sections": ["letterhead", "reference_date", "addressee", "subject", "work_details", "confirmations", "closing", "signatory"],
        "match_patterns": ["*covering letter*", "*bid letter*", "*forwarding letter*", "*tender letter*", "*submission letter*"],
    },
    # ── 8. Financial Turnover Statement ──────────────────────────────────────
    {
        "name": "Financial Turnover Statement",
        "description": "Annual turnover and financial capacity statement with CA certification for eligibility qualification.",
        "document_category": "certificate",
        "structure_json": {
            "sections": ["header", "turnover_table", "average_calculation", "ca_certification", "signatory"],
            "layout": "tabular_certified",
        },
        "content_template_markdown": (
            "# FINANCIAL TURNOVER STATEMENT\n\n"
            "**NIT No.:** [NIT_NUMBER]\n"
            "**Name of Bidder:** [BIDDER_NAME]\n\n"
            "| S.No. | Financial Year | Annual Turnover (₹ Lakhs) | Profit After Tax (₹ Lakhs) |\n"
            "|-------|---------------|--------------------------|---------------------------|\n"
            "| 1 | [FY_1] | [TURNOVER_1] | [PAT_1] |\n"
            "| 2 | [FY_2] | [TURNOVER_2] | [PAT_2] |\n"
            "| 3 | [FY_3] | [TURNOVER_3] | [PAT_3] |\n"
            "| | **Average Annual Turnover** | **[AVG_TURNOVER]** | |\n\n"
            "**Average Annual Turnover (in words):** Rupees [AMOUNT_IN_WORDS] Lakhs only.\n\n"
            "---\n\n"
            "**CHARTERED ACCOUNTANT CERTIFICATION**\n\n"
            "This is to certify that the above financial figures have been verified from the "
            "audited financial statements of M/s [COMPANY_NAME] and are true and correct.\n\n"
            "CA Name: [CA_NAME]\n"
            "Membership No.: [CA_MEMBERSHIP_NO]\n"
            "Firm: [CA_FIRM_NAME]\n"
            "UDIN: [UDIN_NUMBER]\n"
            "Date: [DATE]\n\n"
            "---\n\n"
            "**For and on behalf of [COMPANY_NAME]**\n\n"
            "Signature: ____________________\n"
            "Name: [SIGNATORY_NAME]\n"
            "Designation: [SIGNATORY_DESIGNATION]\n"
            "Date: [DATE]\n"
            "Company Seal\n"
        ),
        "format_rules": [
            "Amounts in Lakhs or Crores with proper Indian number formatting",
            "Must include CA certification with membership number and UDIN",
            "Financial years must match the range specified in the tender eligibility",
            "Average annual turnover must meet the threshold specified in the tender",
            "Include both figures and words for the average turnover amount",
        ],
        "required_sections": ["header", "turnover_table", "average_calculation", "ca_certification", "signatory"],
        "match_patterns": ["*turnover*", "*financial statement*", "*annual turnover*", "*balance sheet*", "*financial capacity*"],
    },
    # ── 9. Power of Attorney / Authorization ─────────────────────────────────
    {
        "name": "Power of Attorney / Authorization",
        "description": "Legal authorization document empowering a person to sign and submit tender documents on behalf of the company.",
        "document_category": "declaration",
        "structure_json": {
            "sections": ["stamp_paper_notice", "preamble", "attorney_details", "scope_of_authority", "validity", "witnesses", "notary"],
            "layout": "legal_document",
        },
        "content_template_markdown": (
            "# POWER OF ATTORNEY\n\n"
            "**(On Non-Judicial Stamp Paper of ₹100/- or appropriate value)**\n\n"
            "**KNOW ALL MEN BY THESE PRESENTS**\n\n"
            "That we, [COMPANY_NAME], a company registered under the Companies Act, "
            "[1956/2013], having its registered office at [FULL_REGISTERED_ADDRESS] "
            "(hereinafter referred to as the \"Principal\"), do hereby irrevocably appoint "
            "and authorize:\n\n"
            "**Name:** [ATTORNEY_NAME]\n"
            "**Designation:** [ATTORNEY_DESIGNATION]\n"
            "**Address:** [ATTORNEY_ADDRESS]\n\n"
            "(hereinafter referred to as the \"Attorney\") as our true and lawful Attorney "
            "to do and execute all or any of the following acts, deeds, and things for and "
            "on our behalf:\n\n"
            "1. To sign, execute, and submit the tender/bid documents for NIT No. [NIT_NUMBER].\n"
            "2. To attend pre-bid meetings and tender opening on our behalf.\n"
            "3. To sign all correspondence, clarifications, and amendments related to the said tender.\n"
            "4. To negotiate terms and conditions on our behalf.\n"
            "5. To do all such acts, deeds, and things as may be necessary in connection with "
            "the above-mentioned tender.\n\n"
            "This Power of Attorney shall remain valid until [VALIDITY_DATE] or until the "
            "completion of all formalities related to the said tender, whichever is later.\n\n"
            "**IN WITNESS WHEREOF**, we have executed this Power of Attorney on this "
            "[DAY] day of [MONTH], [YEAR].\n\n"
            "---\n\n"
            "**For [COMPANY_NAME]**\n\n"
            "Signature: ____________________\n"
            "Name: [AUTHORIZING_SIGNATORY]\n"
            "Designation: [DESIGNATION] (Authorized as per Board Resolution)\n"
            "Company Seal\n\n"
            "**WITNESSES:**\n\n"
            "1. Name: ____________________ Signature: ____________________\n"
            "   Address: ____________________\n\n"
            "2. Name: ____________________ Signature: ____________________\n"
            "   Address: ____________________\n\n"
            "**Notarized by:**\n"
            "Notary Public\n"
        ),
        "format_rules": [
            "Must be executed on non-judicial stamp paper of appropriate value",
            "Requires notarization by a public notary",
            "Must include two witnesses with names, signatures, and addresses",
            "Include full company registration details and registered address",
            "Scope of authority must specifically reference the tender number",
            "Validity period must be clearly stated",
        ],
        "required_sections": ["stamp_paper_notice", "preamble", "attorney_details", "scope_of_authority", "validity", "witnesses", "notary"],
        "match_patterns": ["*power of attorney*", "*authorization*", "*poa*", "*authorised signatory*", "*authorized signatory*"],
    },
    # ── 10. EMD / Bid Security Declaration ───────────────────────────────────
    {
        "name": "EMD / Bid Security Declaration",
        "description": "Declaration in lieu of Earnest Money Deposit (EMD) or bid security, listing disqualification conditions.",
        "document_category": "declaration",
        "structure_json": {
            "sections": ["header", "declaration_preamble", "disqualification_conditions", "validity", "signatory"],
            "layout": "formal_declaration",
        },
        "content_template_markdown": (
            "# BID SECURITY DECLARATION\n"
            "*(In lieu of Earnest Money Deposit)*\n\n"
            "**NIT No.:** [NIT_NUMBER]\n"
            "**Name of Work:** [WORK_NAME]\n\n"
            "**To,**\n"
            "[ADDRESSEE_NAME]\n"
            "[ADDRESSEE_DESIGNATION]\n"
            "[ADDRESSEE_ORGANISATION]\n"
            "[ADDRESSEE_ADDRESS]\n\n"
            "Dear Sir/Madam,\n\n"
            "I/We, the undersigned, declare that:\n\n"
            "I/We understand that, according to the tender conditions, bids must be supported "
            "by a Bid Security Declaration in lieu of Earnest Money Deposit (EMD).\n\n"
            "I/We accept that I/We may be disqualified from bidding for any contract with you "
            "for a period of **[DEBARMENT_PERIOD]** from the date of notification if:\n\n"
            "1. I/We withdraw or modify my/our bid during the period of bid validity specified "
            "in the tender document; or\n\n"
            "2. I/We do not accept the correction of errors in our bid as per the tender provisions; or\n\n"
            "3. Having been notified of the acceptance of our bid by the procuring entity during "
            "the period of bid validity:\n"
            "   (a) I/We fail or refuse to execute the Contract Agreement within the specified time; or\n"
            "   (b) I/We fail or refuse to furnish the Performance Security within the specified time.\n\n"
            "This Bid Security Declaration shall remain valid for a period of [VALIDITY_DAYS] days "
            "from the date of opening of the bid.\n\n"
            "---\n\n"
            "**For and on behalf of [COMPANY_NAME]**\n\n"
            "Signature: ____________________\n"
            "Name: [SIGNATORY_NAME]\n"
            "Designation: [SIGNATORY_DESIGNATION]\n"
            "Date: [DATE]\n"
            "Place: [PLACE]\n"
            "Company Seal\n"
        ),
        "format_rules": [
            "Must reference the specific tender/NIT number",
            "List all disqualification conditions as specified in the tender",
            "Include debarment period as specified by the procuring entity",
            "Bid validity period must match the tender requirement",
            "Include company seal requirement",
        ],
        "required_sections": ["header", "declaration_preamble", "disqualification_conditions", "validity", "signatory"],
        "match_patterns": ["*emd*", "*earnest money*", "*bid security*", "*security deposit*declaration*", "*bid security declaration*"],
    },
]


def seed_format_templates(db: Session) -> int:
    """Seed system format templates if they don't exist. Returns count of created templates."""
    created = 0
    for template_def in SYSTEM_TEMPLATES:
        existing = db.query(DocumentFormatTemplate).filter(
            DocumentFormatTemplate.name == template_def["name"],
            DocumentFormatTemplate.is_system == True,
        ).first()

        if existing:
            continue

        template = DocumentFormatTemplate(
            name=template_def["name"],
            description=template_def["description"],
            document_category=template_def["document_category"],
            structure_json=template_def["structure_json"],
            content_template_markdown=template_def["content_template_markdown"],
            format_rules=template_def["format_rules"],
            required_sections=template_def["required_sections"],
            match_patterns=template_def["match_patterns"],
            is_system=True,
            is_active=True,
        )
        db.add(template)
        created += 1

    if created > 0:
        db.commit()
        logger.info(f"Seeded {created} document format templates")

    return created
