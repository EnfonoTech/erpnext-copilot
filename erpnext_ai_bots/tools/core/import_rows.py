"""Bulk-create draft documents from an uploaded CSV/Excel file.

Flow: read file -> map columns to fields -> try every row inside a savepoint
(dry run) -> report -> only write when the user confirmed (dry_run=false).

Policy: ALL-OR-NOTHING. If any row fails, nothing is written, so a half-finished
import can never be mistaken for a complete one. Drafts only: nothing is
submitted. Permissions are Frappe's own (insert() is called without
ignore_permissions).
"""
import frappe
from erpnext_ai_bots.tools.base import BaseTool

MAX_ROWS = 500
MAX_ERRORS_SHOWN = 20


class ImportRowsTool(BaseTool):
    name = "core.import_rows"
    description = (
        "Import rows from an uploaded CSV or Excel file as new DRAFT documents of one DocType. "
        "All-or-nothing: if any row fails, nothing is created. "
        "ALWAYS call first with dry_run=true, show the user the result (how many rows would be "
        "created, which rows fail and why), and only call again with dry_run=false after the "
        "user clearly confirms. Never submits. Max 500 rows per call."
    )
    parameters = {
        "doctype": {"type": "string", "description": "DocType to create, e.g. Item, Customer"},
        "file_url": {"type": "string", "description": "Uploaded file URL (/private/files/...)"},
        "column_map": {
            "type": "object",
            "description": "Map of file column header -> DocType fieldname, e.g. {\"Part No\": \"item_code\"}",
        },
        "dry_run": {
            "type": "boolean",
            "description": "true = validate only and write nothing (default). false = really create.",
        },
    }
    required_params = ["doctype", "file_url", "column_map"]
    action_type = "Create"
    required_ptype = "create"

    def execute(self, doctype, file_url, column_map, dry_run=True, **kwargs):
        frappe.has_permission(doctype, ptype="create", throw=True)

        from erpnext_ai_bots.tools.core.read_file import ReadFileTool

        data = ReadFileTool(user=self.user, company=self.company).execute(
            file_url, max_rows=MAX_ROWS
        )
        if data.get("error"):
            return data
        if data.get("type") == "excel":
            sheet = next(iter(data["sheets"].values()), None)
        elif data.get("type") == "csv":
            sheet = data
        else:
            return {"error": "Only CSV or Excel files can be imported."}
        if not sheet or not sheet.get("headers"):
            return {"error": "No header row found in the file."}

        headers = sheet["headers"]
        meta = frappe.get_meta(doctype)
        bad_fields = [f for f in column_map.values() if not meta.has_field(f) and f != "name"]
        missing_cols = [c for c in column_map if c not in headers]
        if bad_fields or missing_cols:
            return {
                "error": "Mapping does not match",
                "unknown_fields": bad_fields,
                "columns_not_in_file": missing_cols,
                "file_columns": headers,
            }

        idx = {c: headers.index(c) for c in column_map}
        rows = sheet["rows"][:MAX_ROWS]
        created, errors, names = 0, [], []

        frappe.db.savepoint("ai_import_start")
        for n, row in enumerate(rows, start=2):  # row 1 is the header
            values = {
                column_map[c]: row[i] for c, i in idx.items() if i < len(row) and row[i] != ""
            }
            frappe.db.savepoint("ai_import_row")
            try:
                doc = frappe.get_doc({"doctype": doctype, **values})
                doc.insert()
                created += 1
                names.append(doc.name)
            except Exception as e:
                frappe.db.rollback(save_point="ai_import_row")
                errors.append({"row": n, "error": str(e)[:200]})

        # All-or-nothing: any failed row, or a dry run, rolls everything back.
        if dry_run or errors:
            frappe.db.rollback(save_point="ai_import_start")
            committed = False
        else:
            frappe.db.commit()
            committed = True

        return {
            "doctype": doctype,
            "dry_run": bool(dry_run),
            "rows_in_file": len(rows),
            "created" if committed else "valid_rows": created,
            "failed": len(errors),
            "errors": errors[:MAX_ERRORS_SHOWN],
            "written_to_database": committed,
            "created_names": names[:50] if committed else [],
            "note": (
                "Nothing was written because at least one row failed (all-or-nothing). "
                "Fix the listed rows and run again."
                if errors and not dry_run
                else ""
            ),
        }
