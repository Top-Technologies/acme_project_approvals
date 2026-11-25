from odoo import api, fields, models, Command, _
from odoo.exceptions import UserError, ValidationError


_GUARANTEE_SYNC = object()


class ApprovalCategory(models.Model):
    _inherit = 'approval.category'

    acme_kind = fields.Selection([
        ('bond', 'Performance Bond'),
        ('advance', 'Project Advance Payment'),
        ('guarantee', 'Guarantee Request'),
        ('pm_assignment', 'PM Assignment'),
        ('variation', 'Variation'),
        ('kickoff', 'Team and Kickoff Sign-off'),
        ('work_advance', 'Mobilization Work Advance'),
        ('petty_cash', 'Project Petty Cash'),
        ('project_purchase', 'Outside-BOQ Purchase Request'),
        ('warehouse_request', 'Outside-BOQ Warehouse Requisition'),
    ], string='Project Request Type')


class ApprovalRequest(models.Model):
    _inherit = 'approval.request'

    acme_project_id = fields.Many2one('project.project', string='Project', ondelete='restrict', check_company=True)
    acme_kind = fields.Selection(related='category_id.acme_kind')
    acme_currency_id = fields.Many2one(related='acme_project_id.acme_contract_currency_id')
    acme_amount = fields.Monetary(string='Requested Amount Excluding Tax', currency_field='acme_currency_id', tracking=True)
    acme_contract_reference = fields.Char(string='Contract Reference', copy=False)
    acme_contract_amount = fields.Monetary(related='acme_project_id.acme_contract_amount', currency_field='acme_currency_id')
    acme_bank_id = fields.Many2one('res.bank', string='Requested Bank')
    acme_addressee = fields.Char(string='Bank / Addressee')
    acme_addressee_address = fields.Text(string='Addressee Address')
    acme_beneficiary = fields.Char(string='Payable To')
    acme_account_number = fields.Char(string='Beneficiary Account')
    acme_branch = fields.Char(string='Branch')
    acme_city = fields.Char(string='City')
    acme_remarks = fields.Text(string='Special Remarks')
    acme_invoice_ids = fields.One2many('account.move', 'acme_approval_id', string='Linked Invoices')
    acme_guarantee_id = fields.Many2one('acme.project.guarantee', string='Linked Guarantee', ondelete='set null')

    @api.model_create_multi
    def create(self, vals_list):
        defaults = self.default_get(['acme_project_id', 'category_id'])
        for vals in vals_list:
            project = self.env['project.project'].browse(vals.get('acme_project_id', defaults.get('acme_project_id')))
            category = self.env['approval.category'].browse(vals.get('category_id', defaults.get('category_id')))
            if project and category.acme_kind:
                project.check_access('read')
                vals.setdefault('acme_contract_reference', project.acme_contract_reference)
                project._acme_check_request_slot(category.acme_kind)
        requests = super().create(vals_list)
        for request in requests:
            request.acme_project_id._acme_check_request_slot(request.acme_kind, request.id)
        return requests

    @api.onchange('acme_project_id')
    def _onchange_acme_contract_reference(self):
        if self.request_status == 'new':
            self.acme_contract_reference = self.acme_project_id.acme_contract_reference

    @api.onchange('category_id', 'acme_project_id')
    def _onchange_acme_project(self):
        if self.acme_kind in ('bond', 'guarantee') and self.acme_project_id:
            self.acme_amount = self.acme_project_id.acme_performance_bond_amount

    @api.constrains('acme_project_id', 'category_id', 'acme_amount')
    def _check_acme_request(self):
        for request in self:
            if not request.acme_kind:
                continue
            project = request.acme_project_id
            if not project:
                raise ValidationError(_('A project request requires a project.'))
            if not project.partner_id or not project.company_id or not project.acme_contract_currency_id:
                raise ValidationError(_('The project must have a customer, company and contract currency.'))
            if request.company_id != project.company_id:
                raise ValidationError(_('The approval and project must belong to the same company.'))
            if request.acme_kind in ('pm_assignment', 'variation', 'kickoff', 'work_advance', 'petty_cash') or not request._acme_requires_financial_approval():
                continue
            contract_value = project.acme_contract_total if project.acme_bond_basis == 'total' else project.acme_contract_amount
            if contract_value <= 0:
                raise ValidationError(_('The selected project contract value basis must be positive.'))
            if request.acme_amount <= 0:
                raise ValidationError(_('The requested amount must be positive.'))
            if request.acme_kind == 'bond' and project.acme_contract_currency_id.compare_amounts(request.acme_amount, project.acme_performance_bond_amount):
                raise ValidationError(_('The performance bond request must equal 10% of the selected contract value basis.'))
            if request.acme_amount > project.acme_contract_amount and project.acme_contract_amount > 0:
                raise ValidationError(_('The requested amount cannot exceed the contract value.'))

    def _acme_requires_financial_approval(self):
        return self.acme_kind not in ('project_purchase', 'warehouse_request')

    def action_confirm(self):
        for request in self.filtered('acme_kind'):
            request._check_acme_request()
            request.acme_project_id._acme_check_request_slot(request.acme_kind, request.id)
            if request.acme_kind in ('pm_assignment', 'variation', 'kickoff', 'work_advance', 'petty_cash') or not request._acme_requires_financial_approval():
                continue

            if request.acme_kind == 'guarantee':
                rule = self.env['acme.approval.authority'].matching_rule(
                    request.company_id,
                    request.acme_currency_id or request.company_id.currency_id,
                    request.acme_amount,
                    'guarantee',
                )
                reviewer, authorizer = rule.resolve_users()
                request.approver_ids = [
                    Command.clear(),
                    Command.create({'user_id': reviewer.id, 'required': True, 'sequence': 10}),
                    Command.create({'user_id': authorizer.id, 'required': True, 'sequence': 20}),
                ]

            category_approvers = request.category_id.approver_ids
            expected = 1 if request.acme_kind == 'bond' else 2
            if request.acme_kind == 'guarantee':
                expected = 2
            elif request.acme_kind != 'advance' and (len(category_approvers) < expected or request.category_id.approval_minimum < expected):
                raise UserError(_('Configure at least %(count)s category approver(s) and a minimum of %(count)s approval(s) for %(category)s.',
                                  count=expected, category=request.category_id.name))
            if request.acme_kind == 'guarantee' and not request.category_id.approver_sequence:
                request.category_id.approver_sequence = True

            approvers = request.approver_ids
            required = max(request.category_id.approval_minimum, expected)
            if len(approvers) < required:
                raise UserError(_('Configure all required approvers on this request before submitting.'))
            if request.request_owner_id in approvers.mapped('user_id'):
                raise UserError(_('The requester must not approve their own project request.'))

        return super().action_confirm()

    def action_approve(self, approver=None):
        for request in self.filtered(lambda item: item.acme_kind in ('bond', 'guarantee')).sorted('id'):
            request.check_access('write')
            self.env.cr.execute('UPDATE approval_request SET id = id WHERE id = %s', [request.id])
            request.invalidate_recordset()
        res = super().action_approve(approver=approver)
        for request in self:
            if request.request_status == 'approved' and request.acme_kind in ('guarantee', 'bond') and request.acme_project_id:
                guarantee = self.env['acme.project.guarantee'].search([
                    ('approval_id', '=', request.id)
                ], limit=1)
                if not guarantee:
                    guarantee = self.env['acme.project.guarantee'].with_context(
                        acme_guarantee_sync=_GUARANTEE_SYNC,
                    ).create({
                        'name': request.name or _('Guarantee %s') % request.acme_project_id.name,
                        'project_id': request.acme_project_id.id,
                        'amount': request.acme_amount,
                        'guarantee_type': 'performance',
                        'bank_id': request.acme_bank_id.id if request.acme_bank_id else False,
                        'approval_id': request.id,
                        'state': 'approved',
                    })
                request.acme_guarantee_id = guarantee.id
        return res

    def _acme_can_link_approved_project(self):
        return False

    def write(self, vals):
        financial = {
            'name', 'category_id', 'partner_id', 'reference', 'reason',
            'acme_project_id', 'acme_amount', 'acme_bank_id', 'acme_addressee',
            'acme_addressee_address', 'acme_beneficiary', 'acme_account_number',
            'acme_branch', 'acme_city', 'acme_remarks', 'acme_contract_reference',
        }
        for request in self:
            if (request.acme_kind and financial.intersection(vals) and request.request_status != 'new'
                    and not (set(vals) == {'acme_project_id'} and request._acme_can_link_approved_project())):
                raise UserError(_('Project request details are locked after submission. Create a fresh request for corrections.'))
            if request.acme_invoice_ids and financial.intersection(vals):
                raise UserError(_('An invoiced request cannot be changed.'))
        if {'acme_project_id', 'category_id'}.intersection(vals):
            for request in self:
                project = self.env['project.project'].browse(vals['acme_project_id']) if 'acme_project_id' in vals else request.acme_project_id
                category = self.env['approval.category'].browse(vals['category_id']) if 'category_id' in vals else request.category_id
                project._acme_check_request_slot(category.acme_kind, request.id)
        return super().write(vals)

    def action_acme_create_advance_invoice(self):
        return self._acme_get_advance_certificate()._acme_load_advance_invoice()

    def action_acme_print_request(self):
        self.ensure_one()
        if not self.acme_kind:
            raise UserError(_('This report is for project bond, advance, and guarantee requests.'))
        return self.env.ref('acme_project_approvals.report_project_request').report_action(self)
