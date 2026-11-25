from num2words import num2words

from odoo import api, Command, fields, models, _
from odoo.exceptions import AccessError, UserError
from odoo.tools import float_round


_ADVANCE_WORKFLOW = object()


class AdvanceRequisition(models.Model):
    _inherit = 'approval.request'

    acme_advance_date = fields.Date(string='Requisition Date', default=fields.Date.context_today)
    acme_advance_ref = fields.Char(string='Ref No.', copy=False)
    acme_advance_contract_name = fields.Char(string='Contract Name')
    acme_advance_description = fields.Text(string='Description')
    acme_advance_contract_code = fields.Char(related='acme_project_id.acme_project_sequence')
    acme_advance_type = fields.Selection([('fixed', 'Fixed'), ('percentage', 'Percentage')], readonly=True)
    acme_advance_percent = fields.Float(string='Advance (%)', readonly=True)
    acme_advance_vat_rate = fields.Float(string='VAT Rate (%)', readonly=True)
    acme_advance_vat = fields.Monetary(compute='_compute_advance_totals', currency_field='acme_currency_id')
    acme_advance_total = fields.Monetary(compute='_compute_advance_totals', currency_field='acme_currency_id')
    acme_advance_checked_by = fields.Many2one('res.users', readonly=True, copy=False)
    acme_advance_checked_at = fields.Datetime(readonly=True, copy=False)
    acme_advance_approved_by = fields.Many2one('res.users', readonly=True, copy=False)
    acme_advance_approved_at = fields.Datetime(readonly=True, copy=False)
    acme_advance_authorized_by = fields.Many2one('res.users', readonly=True, copy=False)
    acme_advance_authorized_at = fields.Datetime(readonly=True, copy=False)
    acme_advance_route = fields.Json(readonly=True, copy=False)

    @api.depends('acme_amount', 'acme_advance_vat_rate', 'acme_currency_id')
    def _compute_advance_totals(self):
        for request in self:
            vat = request.acme_amount * request.acme_advance_vat_rate / 100
            request.acme_advance_vat = float_round(vat, precision_digits=2, rounding_method='HALF-UP')
            request.acme_advance_total = request.acme_amount + request.acme_advance_vat

    def _advance_total_in_words(self):
        self.ensure_one()
        cents = round(self.acme_advance_total * 100)
        whole, fraction = divmod(cents, 100)
        unit = 'Birr' if self.acme_currency_id.name == 'ETB' else self.acme_currency_id.name
        return f'{unit} {num2words(whole, lang="en")} and {fraction:02d}/100'

    @api.model
    def _advance_project_defaults(self, project):
        return {
            'acme_advance_type': project.acme_advance_type,
            'acme_advance_percent': project.acme_advance_percent,
            'acme_advance_vat_rate': project.company_id.acme_advance_vat_rate,
            'acme_amount': project.acme_contract_advance_amount,
        }

    @api.onchange('category_id', 'acme_project_id')
    def _onchange_advance_project(self):
        if self.acme_kind == 'advance' and self.acme_project_id:
            project = self.acme_project_id
            self.update(self._advance_project_defaults(project))
            self.partner_id = project.partner_id
            self.acme_advance_contract_name = project.name
            self._onchange_advance_contract_name()

    @api.onchange('acme_advance_contract_name')
    def _onchange_advance_contract_name(self):
        if self.acme_kind == 'advance' and self.acme_advance_contract_name:
            self.acme_advance_description = _('Advance payment for %s', self.acme_advance_contract_name)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._check_advance_audit_write(vals)
            category = self.env['approval.category'].browse(
                vals.get('category_id', self.env.context.get('default_category_id')) or []
            )
            project = self.env['project.project'].browse(
                vals.get('acme_project_id', self.env.context.get('default_acme_project_id')) or []
            )
            if category.acme_kind != 'advance' or not project:
                continue
            defaults = self._advance_project_defaults(project)
            if defaults['acme_advance_type'] == 'fixed':
                defaults['acme_amount'] = vals.get('acme_amount', defaults['acme_amount'])
            vals.update(defaults)
            vals.setdefault('partner_id', project.partner_id.id)
            vals.setdefault('acme_advance_contract_name', project.name)
            vals.setdefault('acme_advance_description', _('Advance payment for %s', vals['acme_advance_contract_name']))
            if not vals.get('acme_advance_ref'):
                vals['acme_advance_ref'] = self.env['ir.sequence'].with_company(project.company_id).next_by_code(
                    'acme.advance.requisition'
                )
        return super().create(vals_list)

    def _check_advance_audit_write(self, vals):
        audit = {'acme_advance_checked_by', 'acme_advance_checked_at', 'acme_advance_approved_by',
                 'acme_advance_approved_at', 'acme_advance_authorized_by',
                 'acme_advance_authorized_at', 'acme_advance_route'}
        if audit.intersection(vals) and self.env.context.get('acme_advance_workflow') is not _ADVANCE_WORKFLOW:
            raise AccessError(_('Use the advance workflow to record decisions.'))

    def write(self, vals):
        self._check_advance_audit_write(vals)
        content = {'acme_advance_date', 'acme_advance_ref', 'acme_advance_contract_name', 'partner_id',
                   'acme_advance_description', 'acme_advance_type', 'acme_advance_percent',
                   'acme_advance_vat_rate', 'approver_ids', 'request_owner_id'}
        advances = self.filtered(lambda record: record.acme_kind == 'advance')
        if (advances and vals.get('request_status') == 'approved'
                and self.env.context.get('acme_advance_workflow') is not _ADVANCE_WORKFLOW):
            raise AccessError(_('Use the configured sequential Advance Request approvals.'))
        for request in advances:
            if request.date_confirmed and content.intersection(vals) and (
                self.env.context.get('acme_advance_workflow') is not _ADVANCE_WORKFLOW
            ):
                raise UserError(_('Submitted advance requisition details are locked.'))
        result = super().write(vals)
        check_fields = (content - {'approver_ids'}) | {
            'acme_project_id', 'category_id', 'acme_amount', 'acme_remarks', 'acme_bank_id',
            'acme_beneficiary', 'acme_account_number', 'acme_branch', 'acme_city',
        }
        if check_fields.intersection(vals):
            checked = advances.filtered(lambda request: not request.date_confirmed and request.acme_advance_checked_by)
            if checked:
                checked.with_context(acme_advance_workflow=_ADVANCE_WORKFLOW).write({
                    'acme_advance_checked_by': False, 'acme_advance_checked_at': False,
                })
        return result

    def action_acme_check_advance(self):
        self.ensure_one()
        self.check_access('write')
        if not self.env.user.has_group('acme_project_core.group_acme_odm'):
            raise AccessError(_('Only the ODM can check an advance requisition.'))
        if self.acme_kind != 'advance' or self.request_status != 'new' or self.request_owner_id == self.env.user:
            raise UserError(_('The ODM can only check another requester’s draft advance requisition.'))
        self.with_context(acme_advance_workflow=_ADVANCE_WORKFLOW).write({
            'acme_advance_checked_by': self.env.uid, 'acme_advance_checked_at': fields.Datetime.now(),
        })

    def _advance_actor(self, position):
        principal = self.env['acme.approval.position.assignment'].for_position(self.company_id, position).user_id
        delegations = self.env['acme.approval.delegation']
        today = fields.Date.context_today(self)
        rows = delegations.search([
            ('company_id', '=', self.company_id.id), ('delegator_position', '=', position),
            ('delegator_id', '=', principal.id), ('effective_from', '<=', today),
            '|', ('effective_to', '=', False), ('effective_to', '>=', today),
        ])
        actors = rows.delegate_id
        if len(actors) > 1:
            raise UserError(_('Resolve overlapping advance approval delegations.'))
        actor = actors or principal
        if not actor.active or self.company_id not in actor.company_ids:
            raise UserError(_('The advance approver must be active and have access to the company.'))
        return actor

    def action_confirm(self):
        self.ensure_one()
        if self.acme_kind != 'advance':
            return super().action_confirm()
        self.check_access('write')
        if self.request_status != 'new' or self.date_confirmed or self.request_owner_id != self.env.user:
            raise UserError(_('Only the requester can submit a new advance requisition.'))
        if not (self.env.user.has_group('acme_project_core.group_acme_odm')
                or self.acme_project_id.user_id == self.env.user):
            raise AccessError(_('Only the assigned PM or an ODM can submit a project advance.'))
        if self.category_id.approval_minimum != 2 or not self.category_id.approver_sequence:
            raise UserError(_('Configure exactly two minimum approvals and enable approver sequence.'))
        self._check_acme_request()
        project = self.acme_project_id
        if project.acme_advance_guarantee_required and not project.acme_guarantee_ids.filtered(
            lambda guarantee: guarantee.guarantee_type == 'advance' and guarantee.state == 'active'
        ):
            raise UserError(_('Register an active advance guarantee on the project before submitting.'))
        terms = self._advance_project_defaults(project)
        if terms['acme_advance_type'] == 'percentage' and self.acme_currency_id.compare_amounts(
            self.acme_amount, terms['acme_amount']
        ):
            raise UserError(_('Refresh the requested amount to match the contract advance percentage.'))
        rule = self.env['acme.approval.authority'].matching_rule(
            self.company_id, self.acme_currency_id, self.acme_amount, 'advance',
        )
        if rule.approver_position != 'dm' or rule.authorizer_position not in ('dgm', 'gm'):
            raise UserError(_('Configure DM approval followed by DGM Operations or GM authorization.'))
        reviewer = self._advance_actor('dm')
        authorizer = self._advance_actor(rule.authorizer_position)
        if len(reviewer | authorizer | self.request_owner_id) != 3:
            raise UserError(_('Requester, DM approver and authorizer must be different users.'))
        request = self.with_context(acme_advance_workflow=_ADVANCE_WORKFLOW)
        request.write({
            'acme_advance_route': {'reviewer': reviewer.id, 'authorizer': authorizer.id},
            'approver_ids': [Command.clear(),
                             Command.create({'user_id': reviewer.id, 'required': True, 'sequence': 10}),
                             Command.create({'user_id': authorizer.id, 'required': True, 'sequence': 20})],
        })
        return super(AdvanceRequisition, request).action_confirm()

    def action_approve(self, approver=None):
        routed = self.filtered(lambda record: record.acme_kind == 'advance')
        if routed.filtered(lambda request: not request.acme_advance_route):
            raise UserError(_('Submit the Advance Request to its configured approvers first.'))
        for request in routed:
            request.check_access('write')
            self.env.cr.execute('UPDATE approval_request SET id = id WHERE id = %s', [request.id])
            request.invalidate_recordset()
            route = request.acme_advance_route
            actor = request.approver_ids.filtered(lambda line: line.user_id == self.env.user)
            if request.request_status != 'pending' or actor.status != 'pending' or self.env.uid not in (
                route['reviewer'], route['authorizer']
            ):
                raise AccessError(_('Only the current designated advance approver can act.'))
        result = super(AdvanceRequisition, self.with_context(
            acme_advance_workflow=_ADVANCE_WORKFLOW
        )).action_approve(approver=None if routed else approver)
        for request in routed.filtered(lambda record: record.acme_advance_route['reviewer'] == self.env.uid):
            request.with_context(acme_advance_workflow=_ADVANCE_WORKFLOW).write({
                'acme_advance_approved_by': self.env.uid, 'acme_advance_approved_at': fields.Datetime.now(),
            })
        for request in routed.filtered(lambda record: record.acme_advance_route['authorizer'] == self.env.uid):
            if request.request_status != 'approved' or not request.acme_advance_approved_by:
                raise UserError(_('The DM must approve before final authorization.'))
            request.with_context(acme_advance_workflow=_ADVANCE_WORKFLOW).write({
                'acme_advance_authorized_by': self.env.uid, 'acme_advance_authorized_at': fields.Datetime.now(),
            })
        return result

    def _action_force_approval(self):
        if self.filtered(lambda request: request.acme_kind == 'advance'):
            raise UserError(_('Advance requisitions require their configured sequential approvals.'))
        return super()._action_force_approval()


class AdvanceRequisitionApprover(models.Model):
    _inherit = 'approval.approver'

    @api.model_create_multi
    def create(self, vals_list):
        approvers = super().create(vals_list)
        approvers._check_advance_route()
        return approvers

    def write(self, vals):
        if {'request_id', 'user_id', 'required', 'sequence'}.intersection(vals) or vals.get('status') == 'approved':
            self._check_advance_route()
            if vals.get('request_id') and self.env['approval.request'].browse(vals['request_id']).acme_advance_route:
                raise AccessError(_('The submitted advance approval route is locked.'))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_advance_route(self):
        self._check_advance_route()

    def _check_advance_route(self):
        if self.env.context.get('acme_advance_workflow') is not _ADVANCE_WORKFLOW and self.request_id.filtered('acme_advance_route'):
            raise AccessError(_('Use the advance approval workflow to record decisions or change routing.'))
