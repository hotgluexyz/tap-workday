"""Singer tap for Workday Finance SOAP reference data."""

from hotglue_singer_sdk import Tap

from tap_workday.streams import (
    CompaniesStream,
    CostCentersStream,
    LedgerAccountsStream,
    SpendCategoriesStream,
    SuppliersStream,
)


class TapWorkday(Tap):
    name = "tap-workday"
    config_jsonschema = {
        "type": "object",
        "required": ["host", "tenant", "username", "password"],
        "properties": {
            "host": {
                "type": "string",
                "description": "HTTPS SOAP host origin, without /ccx/service",
            },
            "tenant": {"type": "string", "minLength": 1},
            "username": {
                "type": "string",
                "minLength": 1,
                "description": "ISU username including @tenant",
            },
            "password": {
                "type": "string",
                "minLength": 1,
                "secret": True,
                "writeOnly": True,
            },
            "api_version": {
                "type": "string",
                "pattern": "^v[0-9]+\\.[0-9]+$",
                "default": "v48.0",
            },
            "page_size": {
                "type": "integer",
                "minimum": 1,
                "maximum": 999,
                "default": 100,
            },
            "request_timeout": {"type": "integer", "minimum": 1, "default": 120},
        },
    }

    def discover_streams(self):
        return [
            stream(tap=self)
            for stream in (
                CompaniesStream,
                CostCentersStream,
                SpendCategoriesStream,
                LedgerAccountsStream,
                SuppliersStream,
            )
        ]


if __name__ == "__main__":
    TapWorkday.cli()
