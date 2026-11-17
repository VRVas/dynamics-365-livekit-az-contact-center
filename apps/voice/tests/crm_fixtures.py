"""Synthetic CRM data shared by the voice cutover tests."""

PHONE = "+12025550101"
REFERENCE = "CAS-01001-T1A2"
CONVERSATION = "11111111-1111-4111-8111-111111111111"


def customer_context(*, phone=PHONE, name="Example Resident", first_name="Example", preferred_language=None):
    return {
        "success": True,
        "source": "dynamics",
        "identityVerified": False,
        "dataScope": "synthetic_profiles_only",
        "customer": {
            "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "name": name,
            "firstName": first_name,
            "preferredLanguage": preferred_language,
            "email": "resident@example.com",
            "phone": phone,
            "context": "Prefers concise service guidance.",
            "account": {
                "accountid": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "name": "Synthetic Account",
                "description": "Synthetic account context.",
            },
        },
        "cases": [{
            "success": True, "source": "dynamics",
            "reference": REFERENCE,
            "caseId": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            "title": "Service request", "status": "In progress",
            "stateCode": 0, "statusCode": 1, "priorityCode": 2,
            "category": "general", "description": "A synthetic service issue.",
            "location": {"district": "Test district", "street": "Test street", "landmark": ""},
            "createdAt": "2026-10-01T12:00:00Z", "updatedAt": "2026-10-02T12:00:00Z",
            "externalSubmission": False,
            "instruction": "Recorded in Dynamics; not an external submission or dispatch.",
        }],
        "casesTruncated": False,
    }


def unknown_customer_context():
    return {
        "success": True, "source": "dynamics",
        "identityVerified": False, "dataScope": "synthetic_profiles_only",
        "customer": None, "cases": [], "casesTruncated": False,
    }


def case_receipt(*, reference=REFERENCE, case_id=None, link_status="not_requested", link_details=None):
    case = customer_context()["cases"][0]
    return {
        **case, "reference": reference, "caseId": case_id or case["caseId"],
        "recentActivities": [],
        "conversationLink": {"status": link_status, **(link_details or {})},
    }
