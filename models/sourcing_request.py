from odoo import api, models, _
from odoo.exceptions import UserError
from odoo.fields import Domain

from .preparation import _SOURCING_KINDS


class ProjectPurchaseRequest(models.Model):
    _inherit = 'approval.request'

    def action_create_purchase_orders(self):
        self.ensure_one()
        if self.acme_kind == 'warehouse_request':
            raise UserError(_('Warehouse requisitions are for internal stock issue.'))
        if self.acme_kind == 'project_purchase':
            self._acme_lock_preparation_request()
            if self.request_status != 'approved' or not self.acme_preparation_signed_at:
                raise UserError(_('Final DM authorization is required before creating RFQs.'))
            if self.acme_project_id.account_id != self.acme_work_analytic_account_id:
                raise UserError(_('The approved project analytic account has changed.'))
            if self.purchase_order_count:
                return self.action_open_purchase_orders()
        return super().action_create_purchase_orders()


class ProjectSourcingProductLine(models.Model):
    _inherit = 'approval.product.line'

    def _acme_check_sourcing_lines(self):
        if self.approval_request_id.filtered(
            lambda request: request.acme_kind in _SOURCING_KINDS and request.acme_preparation_route
        ):
            raise UserError(_('Submitted request product lines are locked.'))

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        lines._acme_check_sourcing_lines()
        return lines

    def write(self, vals):
        if {'approval_request_id', 'product_id', 'quantity', 'product_uom_id'}.intersection(vals):
            self._acme_check_sourcing_lines()
            if vals.get('approval_request_id') and self.env['approval.request'].browse(vals['approval_request_id']).acme_preparation_route:
                raise UserError(_('Submitted project request product lines are locked.'))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_submitted_sourcing(self):
        self._acme_check_sourcing_lines()

    def _get_purchase_orders_domain(self, vendor):
        domain = super()._get_purchase_orders_domain(vendor)
        request = self.approval_request_id
        if request.acme_kind == 'project_purchase':
            orders = request.product_line_ids.purchase_order_line_id.order_id
            domain = Domain.AND([domain, [('project_id', '=', request.acme_project_id.id),
                                         ('id', 'in', orders.ids)]])
        return domain

    def _get_purchase_order_values(self, vendor):
        values = super()._get_purchase_order_values(vendor)
        if self.approval_request_id.acme_kind == 'project_purchase':
            values['project_id'] = self.approval_request_id.acme_project_id.id
        return values
