"""Workday SOAP transport and reference-data extraction."""

from datetime import datetime, timezone
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree as ET

import requests
from defusedxml.ElementTree import fromstring
from hotglue_singer_sdk.streams import Stream
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
WD = "urn:com.workday/bsvc"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
NS = {"wd": WD, "soap": SOAP}


def text(element, path):
    """Read a Workday namespaced field, preserving missing values as null."""
    return element.findtext("/".join(f"wd:{part}" for part in path.split("/")), namespaces=NS)


def reference_ids(element, path):
    """Return typed IDs without relying on their order in the response."""
    return {
        child.get(f"{{{WD}}}type"): child.text for child in element.findall(f"wd:{path}/wd:ID", NS)
    }


def boolean(value):
    """Convert XML Schema boolean values without conflating false and missing."""
    return None if value is None else value in ("1", "true")


class WorkdayStream(Stream):
    """Full-table SOAP stream with a stable snapshot across numbered pages."""

    primary_keys = ["id"]
    replication_key = None
    service = "Financial_Management"
    response_group = {}
    schema = {
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "string", "description": "Workday WID"},
            "reference_id": {"type": ["string", "null"]},
            "name": {"type": ["string", "null"]},
            "references": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string"},
                        "value": {"type": ["string", "null"]},
                    },
                },
            },
            "raw_xml": {
                "type": "string",
                "description": "Complete source object, including relationships not flattened into columns",
            },
        },
    }

    def envelope(self, page, as_of):
        envelope = ET.Element(f"{{{SOAP}}}Envelope")
        header = ET.SubElement(envelope, f"{{{SOAP}}}Header")
        security = ET.SubElement(header, f"{{{WSSE}}}Security", {f"{{{SOAP}}}mustUnderstand": "1"})
        token = ET.SubElement(security, f"{{{WSSE}}}UsernameToken")
        ET.SubElement(token, f"{{{WSSE}}}Username").text = self.config["username"]
        ET.SubElement(
            token,
            f"{{{WSSE}}}Password",
            {
                "Type": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText",
            },
        ).text = self.config["password"]
        body = ET.SubElement(envelope, f"{{{SOAP}}}Body")
        request = ET.SubElement(
            body,
            f"{{{WD}}}{self.operation}_Request",
            {
                f"{{{WD}}}version": self.config["api_version"],
            },
        )
        response_filter = ET.SubElement(request, f"{{{WD}}}Response_Filter")
        for key, value in {
            "As_Of_Effective_Date": as_of[:10],
            "As_Of_Entry_DateTime": as_of,
            "Page": page,
            "Count": self.config["page_size"],
        }.items():
            ET.SubElement(response_filter, f"{{{WD}}}{key}").text = str(value)
        if self.response_group:
            group = ET.SubElement(request, f"{{{WD}}}Response_Group")
            for key, value in self.response_group.items():
                ET.SubElement(group, f"{{{WD}}}{key}").text = value
        return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)

    def parse_records(self, response):
        for element in response.findall(f"wd:Response_Data/wd:{self.record_element}", NS):
            ids = reference_ids(element, f"{self.record_element}_Reference")
            if not ids.get("WID"):
                raise ValueError(
                    f"{self.name}: missing Workday WID; cannot emit a stable primary key"
                )
            record = {
                "id": ids["WID"],
                "reference_id": ids.get(self.reference_type),
                "references": [{"type": key, "value": value} for key, value in ids.items()],
                "raw_xml": ET.tostring(element, encoding="unicode"),
            }
            for field, path in self.fields.items():
                value = text(element, path)
                record[field] = boolean(value) if field in self.boolean_fields else value
            yield record

    boolean_fields = ()

    def get_records(self, context):
        host = self.config["host"].rstrip("/")
        parsed = urlsplit(host)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
        ):
            raise ValueError("host must be an HTTPS origin, without a path or credentials")
        url = f"{host}/ccx/service/{quote(self.config['tenant'], safe='')}/{self.service}/{quote(self.config['api_version'], safe='')}"
        as_of = datetime.now(timezone.utc).isoformat()
        retry = Retry(
            total=4,
            backoff_factor=1,
            status_forcelist=[429, 502, 503, 504],
            allowed_methods=["POST"],
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        with requests.Session() as session:
            session.mount("https://", HTTPAdapter(max_retries=retry))
            page = 1
            while True:
                try:
                    result = session.post(
                        url,
                        data=self.envelope(page, as_of),
                        headers={"Content-Type": "text/xml;charset=UTF-8"},
                        timeout=self.config["request_timeout"],
                        allow_redirects=False,
                    )
                except requests.RequestException:
                    raise RuntimeError(
                        f"{self.operation}: SOAP request failed after bounded retries"
                    ) from None
                try:
                    root = fromstring(result.content)
                except ET.ParseError:
                    raise RuntimeError(
                        f"{self.operation}: non-XML response (HTTP {result.status_code})"
                    ) from None
                fault = root.find("soap:Body/soap:Fault", NS)
                if fault is not None:
                    message = fault.findtext("faultstring") or "SOAP fault"
                    for secret in (self.config["password"], self.config["username"]):
                        message = message.replace(secret, "[REDACTED]")
                    raise RuntimeError(f"{self.operation} ({self.service}): {message}")
                if not 200 <= result.status_code < 300:
                    raise RuntimeError(f"{self.operation}: HTTP {result.status_code}")
                response = root.find(f"soap:Body/wd:{self.operation}_Response", NS)
                if response is None:
                    raise RuntimeError(f"{self.operation}: missing SOAP response element")
                total_pages = text(response, "Response_Results/Total_Pages")
                current_page = text(response, "Response_Results/Page")
                if total_pages is None or current_page is None or int(current_page) != page:
                    raise RuntimeError(
                        f"{self.operation}: missing or inconsistent pagination metadata"
                    )
                yield from self.parse_records(response)
                if page >= int(total_pages):
                    break
                page += 1
