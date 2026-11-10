"""SOAP contract tests with synthetic data; no tenant credentials or fixtures."""

import json
from xml.etree import ElementTree as ET

import jsonschema
import pytest
import responses

from tap_workday.tap import TapWorkday

CONFIG = {
    "host": "https://example.workday.com",
    "tenant": "demo",
    "username": "isu<&@demo",
    "password": "secret<&",
    "page_size": 2,
}
WD = "urn:com.workday/bsvc"
SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
NS = {"wd": WD, "soap": SOAP}


def envelope(operation, data="", page=1, pages=1):
    return f'''<s:Envelope xmlns:s="{SOAP}" xmlns:w="{WD}"><s:Body>
    <w:{operation}_Response><w:Response_Results><w:Total_Pages>{pages}</w:Total_Pages>
    <w:Page>{page}</w:Page></w:Response_Results><w:Response_Data>{data}</w:Response_Data>
    </w:{operation}_Response></s:Body></s:Envelope>'''


@pytest.mark.parametrize(
    "stream,operation,element,ref_type,data,expected",
    [
        (
            "companies",
            "Get_Workday_Companies",
            "Company",
            "Company_Reference_ID",
            "<w:Organization_Data><w:Organization_Name>ACME &amp; Co</w:Organization_Name><w:Organization_Code>001</w:Organization_Code><w:Organization_Active>0</w:Organization_Active></w:Organization_Data>",
            {"name": "ACME & Co", "code": "001", "active": False},
        ),
        (
            "cost_centers",
            "Get_Cost_Centers",
            "Cost_Center",
            "Cost_Center_Reference_ID",
            "<w:Organization_Data><w:Organization_Name>Engineering</w:Organization_Name><w:Organization_Active>true</w:Organization_Active></w:Organization_Data>",
            {"name": "Engineering", "code": None, "active": True},
        ),
        (
            "spend_categories",
            "Get_Resource_Categories",
            "Resource_Category",
            "Spend_Category_ID",
            "<w:Resource_Category_Name>Travel</w:Resource_Category_Name><w:Inactive>false</w:Inactive><w:Expense_Usage>1</w:Expense_Usage>",
            {
                "name": "Travel",
                "inactive": False,
                "expense_usage": True,
                "procurement_usage": None,
            },
        ),
        (
            "suppliers",
            "Get_Suppliers",
            "Supplier",
            "Supplier_Reference_ID",
            "<w:Supplier_Name>Vendor</w:Supplier_Name><w:Supplier_ID>S-001</w:Supplier_ID>",
            {"name": "Vendor", "supplier_id": "S-001"},
        ),
    ],
)
@responses.activate
def test_reference_streams(stream, operation, element, ref_type, data, expected):
    tap = TapWorkday(config=CONFIG)
    source = tap.streams[stream]
    obj = f'''<w:{element}><w:{element}_Reference>
    <w:ID w:type="{ref_type}">REF-1</w:ID><w:ID w:type="WID">wid-1</w:ID>
    </w:{element}_Reference><w:{element}_Data>{data}</w:{element}_Data></w:{element}>'''
    responses.post(
        f"{CONFIG['host']}/ccx/service/demo/{source.service}/v48.0",
        body=envelope(operation, obj),
        content_type="text/xml",
    )
    (record,) = source.get_records(None)
    assert {key: record[key] for key in expected} == expected
    assert record["id"] == "wid-1"
    assert record["reference_id"] == "REF-1"
    assert record["references"] == [
        {"type": ref_type, "value": "REF-1"},
        {"type": "WID", "value": "wid-1"},
    ]
    raw = ET.fromstring(record["raw_xml"])
    assert raw.tag == f"{{{WD}}}{element}"
    assert raw.find(f"wd:{element}_Data", NS) is not None
    jsonschema.validate(record, source.schema)
    request = ET.fromstring(responses.calls[0].request.body)
    assert (
        request.find(
            ".//{http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd}Password"
        ).text
        == CONFIG["password"]
    )


@responses.activate
def test_pagination_and_account_set_scoped_keys():
    source = TapWorkday(config=CONFIG).streams["ledger_accounts"]
    url = f"{CONFIG['host']}/ccx/service/demo/Financial_Management/v48.0"
    for page, set_id in enumerate(("Corporate", "Local"), 1):
        data = f"""<w:Account_Set><w:Account_Set_Data><w:Account_Set_ID>{set_id}</w:Account_Set_ID>
        <w:Ledger_Account_Data><w:Ledger_Account_Identifier>0010</w:Ledger_Account_Identifier>
        <w:Ledger_Account_Name>Cash {set_id}</w:Ledger_Account_Name><w:Retired>{page - 1}</w:Retired>
        </w:Ledger_Account_Data></w:Account_Set_Data></w:Account_Set>"""
        responses.post(url, body=envelope("Get_Account_Sets", data, page, 2))
    records = list(source.get_records(None))
    assert [(r["account_set_id"], r["account_number"], r["retired"]) for r in records] == [
        ("Corporate", "0010", False),
        ("Local", "0010", True),
    ]
    assert [r["name"] for r in records] == ["Cash Corporate", "Cash Local"]
    for record in records:
        jsonschema.validate(record, source.schema)
    requests = [ET.fromstring(call.request.body) for call in responses.calls]
    assert [r.findtext(".//wd:Page", namespaces=NS) for r in requests] == ["1", "2"]
    assert len({r.findtext(".//wd:As_Of_Entry_DateTime", namespaces=NS) for r in requests}) == 1
    assert len({r.findtext(".//wd:As_Of_Effective_Date", namespaces=NS) for r in requests}) == 1


@responses.activate
def test_permission_fault_fails_without_retry_or_credential_leak():
    source = TapWorkday(config=CONFIG).streams["companies"]
    responses.post(
        f"{CONFIG['host']}/ccx/service/demo/Financial_Management/v48.0",
        status=500,
        body=f'''<s:Envelope xmlns:s="{SOAP}"><s:Body><s:Fault>
                   <faultstring>The task submitted is not authorized.</faultstring>
                   </s:Fault></s:Body></s:Envelope>''',
    )
    with pytest.raises(RuntimeError, match="Get_Workday_Companies.*not authorized") as error:
        list(source.get_records(None))
    assert CONFIG["password"] not in str(error.value)
    assert len(responses.calls) == 1


@responses.activate
def test_rate_limit_retry_and_empty_result():
    source = TapWorkday(config=CONFIG).streams["suppliers"]
    url = f"{CONFIG['host']}/ccx/service/demo/Resource_Management/v48.0"
    responses.post(url, status=429, headers={"Retry-After": "0"})
    responses.post(url, body=envelope("Get_Suppliers", pages=0))
    assert list(source.get_records(None)) == []
    assert len(responses.calls) == 2


@responses.activate
def test_missing_pagination_fails_instead_of_truncating():
    source = TapWorkday(config=CONFIG).streams["suppliers"]
    responses.post(
        f"{CONFIG['host']}/ccx/service/demo/Resource_Management/v48.0",
        body=envelope("Get_Suppliers").replace("<w:Total_Pages>1</w:Total_Pages>", ""),
    )
    with pytest.raises(RuntimeError, match="pagination metadata"):
        list(source.get_records(None))


def test_discovery_is_offline_and_full_table():
    catalog = TapWorkday(config=CONFIG).catalog_dict
    assert {s["stream"] for s in catalog["streams"]} == {
        "companies",
        "cost_centers",
        "spend_categories",
        "ledger_accounts",
        "suppliers",
    }
    for stream in catalog["streams"]:
        assert stream["replication_method"] == "FULL_TABLE"
        assert not stream.get("replication_key")
        assert all(key in stream["schema"]["properties"] for key in stream["key_properties"])
    assert CONFIG["password"] not in json.dumps(catalog)
