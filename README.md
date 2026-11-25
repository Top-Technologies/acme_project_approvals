# acme_project_approvals

Version 19.0.2.0.3 for Odoo 19 Enterprise. This remains one component of the existing five-addon suite.

## Approval configuration

The category records remain under `noupdate="1"` deliberately. An upgrade therefore preserves existing categories, configured approvers, their order and other live configuration.

After installation or upgrade, open **Approvals > Configuration > Approval Types**:

* **Project Performance Bond**: configure the authorized DM as a category approver and set **Minimum Approval** to 1.
* **Project Advance Payment**: configure DM first and DGM second, set **Minimum Approval** to 2, and enable **Approvers Sequence**.

Requests cannot be submitted when these settings are missing or insufficient, or when the requester is one of the approvers. Do not configure employee names in module data.

## Upgrade

Back up the database, update the apps list, then upgrade `acme_project_approvals` (for example with `odoo-bin -d <database> -u acme_project_approvals --stop-after-init`). Do not uninstall the addon. Existing requests, categories and approver configuration are retained.

The requested Word templates were not present in the addon or custom-addons tree during this revision. The reports therefore implement confirmed field and workflow corrections only; no claim of pixel/content alignment to unavailable templates is made.
