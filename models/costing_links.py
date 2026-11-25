from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from .preparation import _STAFF_ADVANCE_KINDS


def _with_project_analytic(record, project):
    distribution = record.analytic_distribution or {}
    if not distribution:
        return project._get_analytic_distribution()
    account_ids = {int(account) for key in distribution for account in key.split(',')}
    used_plans = record.env['account.analytic.account'].browse(account_ids).root_plan_id
    missing = project._get_analytic_accounts().filtered(lambda account: account.root_plan_id not in used_plans)
    if not missing:
        return distribution
    suffix = ','.join(map(str, missing.ids))
    return {f'{key},{suffix}': percent for key, percent in distribution.items()}


class ProjectPurchaseCost(models.Model):
    _inherit = 'purchase.order'

    @api.constrains('project_id', 'company_id')
    def _check_acme_purchase_project_company(self):
        for order in self.filtered('project_id'):
            if order.project_id.company_id and order.project_id.company_id != order.company_id:
                raise ValidationError(_('The project and purchase order must belong to the same company.'))


class ProjectPurchaseCostLine(models.Model):
    _inherit = 'purchase.order.line'

    def _prepare_account_move_line(self, move=False):
        values = super()._prepare_account_move_line(move=move)
        if self.order_id.project_id and not self.display_type:
            values['analytic_distribution'] = _with_project_analytic(self, self.order_id.project_id)
        return values


class VendorProjectBill(models.Model):
    _inherit = 'account.move'

    acme_vendor_project_id = fields.Many2one(
        'project.project', string='Project for Direct Vendor Costs', check_company=True, ondelete='restrict',
    )

    acme_vendor_source_project_ids = fields.Many2many(
        'project.project', compute='_compute_acme_vendor_source_projects', string='Projects from Purchase Orders',
    )

    @api.depends('invoice_line_ids.purchase_line_id.order_id.project_id')
    def _compute_acme_vendor_source_projects(self):
        for move in self:
            move.acme_vendor_source_project_ids = move.invoice_line_ids.purchase_line_id.order_id.project_id

    @api.constrains('acme_vendor_project_id', 'move_type')
    def _check_vendor_cost_project(self):
        for bill in self.filtered('acme_vendor_project_id'):
            if bill.move_type not in ('in_invoice', 'in_refund', 'in_receipt') or not bill.acme_vendor_project_id.account_id:
                raise ValidationError(_('Select a project with an analytic account on a vendor bill, receipt or credit note.'))

    def write(self, vals):
        if 'acme_vendor_project_id' in vals and any(move.state == 'posted' for move in self):
            raise UserError(_('The direct vendor project is locked after posting.'))
        return super().write(vals)


class VendorProjectBillLine(models.Model):
    _inherit = 'account.move.line'

    @api.depends('move_id.acme_vendor_project_id')
    def _compute_analytic_distribution(self):
        super()._compute_analytic_distribution()
        for line in self.filtered(lambda item: item.display_type == 'product' and not item.purchase_line_id
                                  and item.move_id.state == 'draft' and item.move_id.acme_vendor_project_id):
            line.analytic_distribution = _with_project_analytic(line, line.move_id.acme_vendor_project_id)


class StaffAdvanceExpense(models.Model):
    _inherit = 'hr.expense'

    acme_staff_advance_id = fields.Many2one(
        'approval.request', string='Authorized Staff Advance', check_company=True, ondelete='restrict', copy=False,
    )

    @api.depends('acme_staff_advance_id')
    def _compute_analytic_distribution(self):
        super()._compute_analytic_distribution()
        for expense in self.filtered(lambda line: line.acme_staff_advance_id and not line.account_move_id):
            expense.analytic_distribution = _with_project_analytic(expense, expense.acme_staff_advance_id.acme_project_id)

    @api.constrains('acme_staff_advance_id', 'employee_id', 'analytic_distribution', 'payment_mode')
    def _check_advance_expense(self):
        for expense in self.filtered('acme_staff_advance_id'):
            request = expense.acme_staff_advance_id
            account = request.acme_work_analytic_account_id
            allocations = expense.analytic_distribution or {}
            if (request.acme_kind not in _STAFF_ADVANCE_KINDS or request.request_status != 'approved'
                    or expense.employee_id not in request._acme_staff_payee() or expense.payment_mode != 'own_account' or not account
                    or not any(str(account.id) in key.split(',') for key in allocations)):
                raise ValidationError(_('Use your authorized staff advance and retain its approved project analytic account.'))

    def write(self, vals):
        if 'acme_staff_advance_id' in vals and self.filtered(lambda expense: expense.account_move_id.state == 'posted'):
            raise UserError(_('Posted settlement expenses retain their original advance link.'))
        return super().write(vals)


class StaffAdvanceRequest(models.Model):
    _inherit = 'approval.request'

    def action_acme_advance_expenses(self):
        self.ensure_one()
        self.check_access('read')
        if self.acme_kind not in _STAFF_ADVANCE_KINDS or self.request_status != 'approved':
            raise UserError(_('An authorized mobilization advance is required.'))
        return {
            'type': 'ir.actions.act_window', 'name': _('Linked Staff Advance Expenses'),
            'res_model': 'hr.expense', 'view_mode': 'list,form', 'domain': [('acme_staff_advance_id', '=', self.id)],
            'context': {'default_acme_staff_advance_id': self.id, 'project_id': self.acme_project_id.id},
        }
