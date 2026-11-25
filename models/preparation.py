import math

from odoo import api, Command, fields, models, _
from odoo.exceptions import AccessError, UserError
from odoo.tools import is_html_empty


_SOURCING_KINDS = ('project_purchase', 'warehouse_request')
_STAFF_ADVANCE_KINDS = ('work_advance', 'petty_cash')
_KINDS = ('kickoff', *_STAFF_ADVANCE_KINDS, *_SOURCING_KINDS)
_PREPARATION_TOKEN = object()
_AUDIT = {'acme_preparation_route', 'acme_kickoff_snapshot', 'acme_preparation_signed_by_id',
          'acme_preparation_signed_at', 'acme_work_analytic_account_id'}


class ProjectPreparationApproval(models.Model):
    _inherit = 'project.project'

    def action_acme_kickoff_signoff(self):
        return self._acme_open_preparation_request('kickoff')

    def action_acme_work_advance(self):
        return self._acme_open_preparation_request('work_advance')

    def action_acme_petty_cash(self):
        return self._acme_open_preparation_request('petty_cash')

    def action_acme_project_purchase(self):
        return self._acme_open_preparation_request('project_purchase')

    def action_acme_warehouse_request(self):
        return self._acme_open_preparation_request('warehouse_request')

    def _acme_open_preparation_request(self, kind):
        self.ensure_one()
        self._acme_check_preparation_editor()
        if kind != 'kickoff' and self.user_id != self.env.user:
            raise AccessError(_('Only the assigned PM can submit this project request.'))
        if kind == 'kickoff':
            self._acme_preparation_snapshot()
        category = self.env['approval.category'].search([
            ('acme_kind', '=', kind), ('company_id', '=', self.company_id.id),
        ])
        if len(category) != 1:
            raise UserError(_('Configure exactly one active approval type for this workflow and company.'))
        return {
            'type': 'ir.actions.act_window', 'res_model': 'approval.request', 'view_mode': 'form',
            'context': {'default_acme_project_id': self.id, 'default_category_id': category.id,
                        'default_name': _('%(category)s: %(project)s', category=category.name, project=self.name),
                        **({'default_acme_staff_employee_id': self.env['hr.employee'].search([
                            ('company_id', '=', self.company_id.id), ('user_id', '=', self.env.uid),
                        ], limit=1).id} if kind in _STAFF_ADVANCE_KINDS else {})},
        }


class PreparationRequest(models.Model):
    _inherit = 'approval.request'

    acme_preparation_route = fields.Json(readonly=True, copy=False)
    acme_kickoff_snapshot = fields.Json(readonly=True, copy=False)
    acme_preparation_signed_by_id = fields.Many2one('res.users', string='Signed Off By', readonly=True, copy=False)
    acme_preparation_signed_at = fields.Datetime(string='Signed Off At', readonly=True, copy=False)
    acme_work_analytic_account_id = fields.Many2one('account.analytic.account', readonly=True, copy=False, ondelete='restrict')
    acme_work_purpose = fields.Selection([
        ('per_diem', 'Per Diem'), ('procurement', 'On-site Procurement'),
        ('loading', 'Loading'), ('unloading', 'Unloading'), ('labor', 'Other Labor'),
        ('mixed', 'Multiple Mobilization Costs'),
    ], string='Mobilization Purpose')

    @api.model_create_multi
    def create(self, vals_list):
        if any(_AUDIT.intersection(vals) for vals in vals_list) or any(
            self.env.context.get(f'default_{field}') for field in _AUDIT
        ):
            raise AccessError(_('Use the workflow to record approval decisions.'))
        return super().create(vals_list)

    def write(self, vals):
        if self.env.context.get('acme_preparation_token') is not _PREPARATION_TOKEN:
            if _AUDIT.intersection(vals):
                raise AccessError(_('Use the workflow to record approval decisions.'))
            locked = {'acme_project_id', 'acme_amount', 'acme_work_purpose', 'reason', 'category_id',
                      'request_owner_id', 'approver_ids', 'product_line_ids', 'acme_staff_employee_id'}
            if locked.intersection(vals) and self.filtered('acme_preparation_route'):
                raise UserError(_('Submitted team plans and mobilization advances are locked.'))
        return super().write(vals)

    def _acme_preparation_kinds(self):
        return _KINDS

    def _acme_preparation_actors(self, positions):
        actors = self.env['res.users']
        for position in positions:
            actor = self.env['acme.approval.position.assignment'].for_position(self.company_id, position).user_id
            if not actor.active or self.company_id not in actor.company_ids or not actor.has_group(
                f'acme_project_core.group_acme_{position}'
            ):
                raise UserError(_('Configure an active %(role)s with access to the project company.', role=position.upper()))
            if actor in actors:
                raise UserError(_('Configure different users for the approval positions.'))
            actors |= actor
        return actors

    def _acme_extra_preparation_submission(self):
        return None

    def _acme_validate_preparation_decision(self):
        if self.acme_kind == 'kickoff':
            if not self.env.user.has_group('acme_project_core.group_acme_dm'):
                raise AccessError(_('Only the designated DM can sign off the team and kickoff plan.'))
            submitted = {key: value for key, value in (self.acme_kickoff_snapshot or {}).items()
                         if key != 'work_packages'}
            if submitted != self.acme_project_id._acme_preparation_snapshot():
                raise UserError(_('The team or kickoff changed. Submit a fresh sign-off request.'))
        if self.acme_kind in _SOURCING_KINDS:
            position = 'odm' if self.env.uid == self.acme_preparation_route[0] else 'dm'
            if not self.env.user.has_group(f'acme_project_core.group_acme_{position}'):
                raise AccessError(_('Only the designated Operations role may approve this request.'))

    def _acme_complete_preparation(self):
        if self.acme_kind in _STAFF_ADVANCE_KINDS:
            self._acme_notify_work_advance_finance()

    def action_confirm(self):
        self.ensure_one()
        if self.acme_kind not in self._acme_preparation_kinds():
            return super().action_confirm()
        self._acme_lock_preparation_request()
        self._check_acme_request()
        project = self.acme_project_id
        if self.acme_preparation_route or self.request_status != 'new' or self.request_owner_id != self.env.user:
            raise UserError(_('Only the requester can submit a new request.'))
        project._acme_check_preparation_editor()
        values = {}
        extra = self._acme_extra_preparation_submission()
        if extra is not None:
            values, actors = extra
        elif self.acme_kind == 'kickoff':
            values['acme_kickoff_snapshot'] = project._acme_preparation_snapshot()
            actors = self.env['acme.approval.position.assignment'].for_position(self.company_id, 'dm').user_id
            if not actors.active or self.company_id not in actors.company_ids or not actors.has_group(
                'acme_project_core.group_acme_dm'
            ):
                raise UserError(_('Configure an active DM with access to the project company.'))
        elif self.acme_kind in _SOURCING_KINDS:
            if project.user_id != self.env.user:
                raise AccessError(_('Only the assigned PM can request outside-BOQ materials.'))
            if not project.account_id or is_html_empty(self.reason) or not self.product_line_ids or any(
                not line.product_id or not math.isfinite(line.quantity) or line.quantity <= 0
                for line in self.product_line_ids
            ):
                raise UserError(_('Set the project analytic account and enter products, positive quantities, and a reason.'))
            if (self.acme_kind == 'project_purchase') != (self.approval_type == 'purchase'):
                raise UserError(_('Enable Create RFQs only on the Outside-BOQ Purchase Request approval type.'))
            values['acme_work_analytic_account_id'] = project.account_id.id
            actors = self._acme_preparation_actors(('odm', 'dm'))
        else:
            if project.user_id != self.env.user:
                raise AccessError(_('Only the assigned PM can request a project staff advance.'))
            employee = self._acme_staff_payee()
            if len(employee) != 1 or not employee.active or not employee.user_id or not employee.work_contact_id:
                raise UserError(_('Select an active employee payee with an internal user and a work contact.'))
            if (not project.account_id or is_html_empty(self.reason)
                    or (self.acme_kind == 'work_advance' and not self.acme_work_purpose)):
                raise UserError(_('Set the project analytic account and reason, and select the purpose for a work advance.'))
            values['acme_staff_employee_id'] = employee.id
            if not math.isfinite(self.acme_amount) or self.acme_amount <= 0:
                raise UserError(_('Enter a positive mobilization amount.'))
            values['acme_work_analytic_account_id'] = project.account_id.id
            rule = self.env['acme.approval.authority'].matching_rule(
                self.company_id, self.acme_currency_id, self.acme_amount, self.acme_kind,
            )
            if (rule.approver_position, rule.authorizer_position) not in (('odm', 'dm'), ('dm', 'dgm')):
                raise UserError(_('Configure ODM then DM within the limit, or DM then DGM Operations above it.'))
            reviewer = self._advance_actor(rule.approver_position)
            authorizer = self._advance_actor(rule.authorizer_position)
            if reviewer == authorizer:
                raise UserError(_('Review and authorization must be assigned to different users.'))
            actors = reviewer | authorizer
        if self.env.user in actors:
            raise UserError(_('The requester cannot approve their own request.'))
        if (self.category_id.approval_minimum != len(actors) or self.category_id.manager_approval
                or (len(actors) == 2 and not self.category_id.approver_sequence)):
            raise UserError(_('Configure the approval type with the required minimum, sequence, and no automatic manager approver.'))
        request = self.with_context(acme_preparation_token=_PREPARATION_TOKEN)
        request.write(dict(values, acme_preparation_route=actors.ids, approver_ids=[
            Command.clear(), *[Command.create({'user_id': actor.id, 'required': True, 'sequence': index * 10})
                               for index, actor in enumerate(actors, start=1)],
        ]))
        return super(PreparationRequest, request).action_confirm()

    def _acme_lock_preparation_request(self):
        self.ensure_one()
        self.check_access('write')
        self.env.cr.execute('UPDATE approval_request SET id = id WHERE id = %s', [self.id])
        self.invalidate_recordset()

    def action_approve(self, approver=None):
        kinds = self._acme_preparation_kinds()
        if not self.filtered(lambda request: request.acme_kind in kinds):
            return super().action_approve(approver=approver)
        self._acme_lock_preparation_request()
        route = self.acme_preparation_route or []
        rows = {row.user_id.id: row.status for row in self.approver_ids}
        expected = next((user for user in route if rows.get(user) != 'approved'), None)
        if self.request_status != 'pending' or expected != self.env.uid or rows.get(expected) != 'pending':
            raise AccessError(_('Only the designated current approver may act.'))
        self._acme_validate_preparation_decision()
        request = self.with_context(acme_preparation_token=_PREPARATION_TOKEN)
        result = super(PreparationRequest, request).action_approve()
        if request.request_status == 'approved':
            request.write({'acme_preparation_signed_by_id': self.env.uid,
                           'acme_preparation_signed_at': fields.Datetime.now()})
            request._acme_complete_preparation()
        return result

    def _acme_notify_work_advance_finance(self):
        finance = self.company_id.acme_work_advance_finance_user_id
        project = self.acme_project_id
        if finance.active and not finance.share and self.company_id in finance.company_ids and project.with_user(finance).has_access('read'):
            project.activity_schedule(
                'mail.mail_activity_data_todo', user_id=finance.id,
                summary=_('Finance: Authorized %s', self.category_id.name),
                note=_('Process authorized request %(request)s through the existing Finance payment process. Amount: %(amount)s %(currency)s.',
                       request=self.display_name, amount=self.acme_amount, currency=self.acme_currency_id.name),
            )
        else:
            self.message_post(body=_('Finance handoff pending: configure a Finance reviewer with access to this project.'))

    def _action_force_approval(self):
        kinds = self._acme_preparation_kinds()
        if self.filtered(lambda request: request.acme_kind in kinds):
            raise UserError(_('Team sign-off and work advances require their designated approvals.'))
        return super()._action_force_approval()


class PreparationApprover(models.Model):
    _inherit = 'approval.approver'

    def _acme_check_preparation_approver_lock(self):
        if self.env.context.get('acme_preparation_token') is not _PREPARATION_TOKEN and self.request_id.filtered('acme_preparation_route'):
            raise AccessError(_('Use the designated workflow to change or approve this request.'))

    @api.model_create_multi
    def create(self, vals_list):
        rows = super().create(vals_list)
        rows._acme_check_preparation_approver_lock()
        return rows

    def write(self, vals):
        if {'request_id', 'user_id', 'sequence', 'required'}.intersection(vals) or vals.get('status') == 'approved':
            self._acme_check_preparation_approver_lock()
            if vals.get('request_id') and self.env['approval.request'].browse(vals['request_id']).acme_preparation_route:
                raise AccessError(_('Submitted approval routing is locked.'))
        if 'status' in vals and self.request_id.filtered('acme_preparation_signed_at'):
            raise UserError(_('A completed sign-off is historical. Submit a new request for corrections.'))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_preparation_route(self):
        self._acme_check_preparation_approver_lock()
