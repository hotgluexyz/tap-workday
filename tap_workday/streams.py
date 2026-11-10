"""Reference data for coding Corpay transactions in Workday Finance."""

from xml.etree import ElementTree as ET

from tap_workday.client import NS, WorkdayStream, boolean, reference_ids, text


class CompaniesStream(WorkdayStream):
    name = "companies"
    operation = "Get_Workday_Companies"
    record_element = "Company"
    reference_type = "Company_Reference_ID"
    fields = {
        "name": "Company_Data/Organization_Data/Organization_Name",
        "code": "Company_Data/Organization_Data/Organization_Code",
        "active": "Company_Data/Organization_Data/Organization_Active",
    }
    boolean_fields = ("active",)
    schema = {
        **WorkdayStream.schema,
        "properties": {
            **WorkdayStream.schema["properties"],
            "code": {"type": ["string", "null"]},
            "active": {"type": ["boolean", "null"]},
        },
    }


class CostCentersStream(CompaniesStream):
    name = "cost_centers"
    operation = "Get_Cost_Centers"
    record_element = "Cost_Center"
    reference_type = "Cost_Center_Reference_ID"
    response_group = {"Include_Reference": "true", "Include_Cost_Center_Data": "true"}
    fields = {
        field: path.replace("Company_Data/", "Cost_Center_Data/")
        for field, path in CompaniesStream.fields.items()
    }


class SpendCategoriesStream(WorkdayStream):
    name = "spend_categories"
    operation = "Get_Resource_Categories"
    record_element = "Resource_Category"
    reference_type = "Spend_Category_ID"
    response_group = {"Include_Reference": "true"}
    fields = {
        "name": "Resource_Category_Data/Resource_Category_Name",
        "inactive": "Resource_Category_Data/Inactive",
        "expense_usage": "Resource_Category_Data/Expense_Usage",
        "procurement_usage": "Resource_Category_Data/Procurement_Usage",
    }
    boolean_fields = ("inactive", "expense_usage", "procurement_usage")
    schema = {
        **WorkdayStream.schema,
        "properties": {
            **WorkdayStream.schema["properties"],
            **{field: {"type": ["boolean", "null"]} for field in boolean_fields},
        },
    }


class SuppliersStream(WorkdayStream):
    name = "suppliers"
    service = "Resource_Management"
    operation = "Get_Suppliers"
    record_element = "Supplier"
    reference_type = "Supplier_Reference_ID"
    response_group = {"Include_Reference": "true"}
    fields = {
        "name": "Supplier_Data/Supplier_Name",
        "supplier_id": "Supplier_Data/Supplier_ID",
    }
    schema = {
        **WorkdayStream.schema,
        "properties": {
            **WorkdayStream.schema["properties"],
            "supplier_id": {"type": ["string", "null"]},
        },
    }


class LedgerAccountsStream(WorkdayStream):
    """Ledger accounts are nested in account sets, not a Get_Ledger_Accounts API."""

    name = "ledger_accounts"
    operation = "Get_Account_Sets"
    primary_keys = ["account_set_id", "account_number"]
    schema = {
        "type": "object",
        "required": ["account_set_id", "account_number"],
        "properties": {
            **{
                field: {"type": ["string", "null"]}
                for field in (
                    "account_set_name",
                    "name",
                    "account_type",
                    "raw_xml",
                )
            },
            "account_set_id": {"type": "string"},
            "account_number": {"type": "string"},
            "retired": {"type": ["boolean", "null"]},
        },
    }

    def parse_records(self, response):
        for account_set in response.findall("wd:Response_Data/wd:Account_Set", NS):
            ids = reference_ids(account_set, "Account_Set_Reference")
            data = account_set.find("wd:Account_Set_Data", NS)
            if data is None:
                raise ValueError("ledger_accounts: missing Account_Set_Data")
            set_id = ids.get("Account_Set_ID") or text(data, "Account_Set_ID")
            for account in data.findall("wd:Ledger_Account_Data", NS):
                number = text(account, "Ledger_Account_Identifier")
                if not set_id or not number:
                    raise ValueError("ledger_accounts: missing account set or account number")
                yield {
                    "account_set_id": set_id,
                    "account_set_name": text(data, "Account_Set_Name"),
                    "account_number": number,
                    "name": text(account, "Ledger_Account_Name"),
                    "account_type": reference_ids(account, "Ledger_Account_Type_Reference").get(
                        "Ledger_Account_Type_ID"
                    ),
                    "retired": boolean(text(account, "Retired")),
                    "raw_xml": ET.tostring(account, encoding="unicode"),
                }
