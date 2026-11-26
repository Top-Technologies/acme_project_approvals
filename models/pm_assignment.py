import json

from odoo import api, Command, fields, models, _
from odoo.exceptions import AccessError, UserError


_PM_WORKFLOW = object()


class ProjectHandoverTask(models.Model):
    _inherit = 'project.task'

    acme_is_handover_task = fields.Boolean(compute='_compute_acme_is_handover_task')

    @api.depends('project_id.acme_handover_task_id')
    def _compute_acme_is_handover_task(self):
        for task in self:
            task.acme_is_handover_task = task == task.project_id.acme_handover_task_id


class ProjectPMAssignment(models.Model):
    _inherit = 'project.project'

    acme_handover_duration_days = fields.Integer(string='Planned Duration (Days)', compute='_compute_handover_duration')

    @api.depends('date_start', 'date')
    def _compute_handover_duration(self):
        for project in self:
            start, end = fields.Date.to_date(project.date_start), fields.Date.to_date(project.date)
            project.acme_handover_duration_days = (end - start).days if start and end else 0

    def _acme_pending_pm_request(self):
        return self.env['approval.request'].search([
            ('acme_project_id', 'in', self.ids), ('acme_kind', '=', 'pm_assignment'),
            ('request_status', 'in', ['new', 'pending']),
        ], limit=1)

    @api.model_create_multi
    def create(self, vals_list):
        default_candidate = self.env.context.get('default_acme_pm_candidate_id')
        if any(vals.get('acme_pm_candidate_id', default_candidate) for vals in vals_list):
            raise UserError(_('Verify the project before nominating a PM.'))
        return super().create(vals_list)

    def write(self, vals):
        if 'acme_pm_candidate_id' in vals:
            self._check_lifecycle_for_handover()
            if not self.env.user.has_group('acme_project_core.group_acme_odm'):
                raise AccessError(_('Only the ODM can nominate a Project Manager.'))
            if self._acme_pending_pm_request():
                raise UserError(_('Resolve the pending PM assignment before changing the candidate.'))
        if self.env.context.get('acme_pm_workflow') is not _PM_WORKFLOW:
            if 'acme_handover_task_id' in vals or (
                'user_id' in vals and self.filtered(
                    lambda project: project.acme_lifecycle_state != 'draft' or project.acme_order_id
                )
            ):
                self._check_lifecycle_for_handover()
                raise UserError(_('Use the PM assignment approval to change the manager or handover task.'))
        return super().write(vals)

    def _acme_pm_registration_snapshot(self):
        self.ensure_one()
        return json.dumps({
            'candidate': self.acme_pm_candidate_id.id,
            'code': self.acme_project_sequence,
            'currency': self.acme_contract_currency_id.name,
            'contract_value_excluding_tax': self.acme_contract_amount,
            'contract_value_including_tax': self.acme_contract_total,
            'start': str(self.date_start or ''),
            'end': str(self.date or ''),
            'advance_received_excluding_tax': self.acme_advance_received,
            'contract_advance': self.acme_contract_advance_amount,
            'milestones': [
                [milestone.id, milestone.name, str(milestone.deadline or '')]
                for milestone in self.milestone_ids.sorted('id')
            ],
        }, ensure_ascii=False)

    def action_acme_submit_pm_assignment(self):
        self.ensure_one()
        self.check_access('write')
        self._check_lifecycle_for_handover()
        if not self.env.user.has_group('acme_project_core.group_acme_odm'):
            raise AccessError(_('Only the ODM can confirm registration and submit the PM assignment.'))
        self.env.cr.execute('UPDATE project_project SET id = id WHERE id = %s', [self.id])
        self.invalidate_recordset()
        self._check_lifecycle_for_handover()
        if self._acme_pending_pm_request():
            raise UserError(_('A PM assignment is already pending for this project.'))
        candidate = self.acme_pm_candidate_id
        if not candidate or not candidate.active or self.company_id not in candidate.company_ids:
            raise UserError(_('Select an active PM candidate with access to the project company.'))
        dm = self.env['acme.approval.position.assignment'].for_position(self.company_id, 'dm').user_id
        if not dm.active or self.company_id not in dm.company_ids or not dm.has_group(
            'acme_project_core.group_acme_dm'
        ):
            raise UserError(_('Configure an active DM with access to the project company.'))
        if dm == self.env.user or dm == candidate:
            raise UserError(_('The assigned DM must differ from the ODM and PM candidate.'))
        category = self.env['approval.category'].search([
            ('company_id', '=', self.company_id.id), ('acme_kind', '=', 'pm_assignment'),
        ])
        if len(category) != 1 or category.approval_minimum != 1:
            raise UserError(_('Configure one active PM Assignment approval type with minimum approvals set to one for this company.'))
        company = self.company_id
        if not company.acme_operations_partner_ids or not company.acme_handover_partner_ids:
            raise UserError(_('Configure Operations recipients and department followers on the project company.'))
        task = self._acme_get_handover_task()
        request = self._acme_submit_handover(category, dm)
        self.message_subscribe(partner_ids=company.acme_handover_partner_ids.ids)
        self.message_post(body=_('ODM confirmed registration and submitted PM assignment and handover to %s.', dm.display_name))
        task.message_post(
            body=_('ODM confirmed registration and submitted the PM assignment for DM approval.'),
            partner_ids=company.acme_operations_partner_ids.ids,
            subtype_xmlid='mail.mt_comment',
        )
        activity = task.activity_schedule(
            'mail.mail_activity_data_todo', user_id=dm.id, summary=_('Review PM Assignment and Handover'),
            note=_('Approve the linked PM Assignment request to confirm the PM and complete the handover.'),
        )
        request.with_context(acme_pm_workflow=_PM_WORKFLOW).acme_pm_handover_activity_id = activity
        request.with_context(acme_pm_workflow=_PM_WORKFLOW).action_confirm()
        return {'type': 'ir.actions.act_window', 'res_model': 'approval.request',
                'view_mode': 'form', 'res_id': request.id}

    def action_acme_open_handover(self):
        self.ensure_one()
        self.check_access('read')
        task = self.acme_handover_task_id
        if not task:
            self.check_access('write')
            self._check_lifecycle_for_handover()
            if not self.env.user.has_group('acme_project_core.group_acme_odm'):
                raise AccessError(_('Only the ODM can prepare the handover.'))
            self.env.cr.execute('UPDATE project_project SET id = id WHERE id = %s', [self.id])
            self.invalidate_recordset()
            self._check_lifecycle_for_handover()
            task = self._acme_get_handover_task()
        task.check_access('read')
        return {'type': 'ir.actions.act_window', 'res_model': 'project.task',
                'view_mode': 'form', 'res_id': task.id}

    def _acme_get_handover_task(self):
        template = self.company_id.acme_handover_template_id
        task = self.acme_handover_task_id or self.task_ids.filtered(
            lambda item: template and item.name == template.name and not item.is_template
        )
        if len(task) > 1:
            raise UserError(_('Multiple matching Handover tasks exist. Resolve them before submitting.'))
        if not task:
            if not template or not template.is_template:
                raise UserError(_('Configure a Handover Task Template on the project company.'))
            task = template.with_context(copy_from_template=True).copy({
                'project_id': self.id, 'parent_id': False, 'partner_id': self.partner_id.id,
                'user_ids': [Command.clear()],
            })
        self.with_context(acme_pm_workflow=_PM_WORKFLOW).acme_handover_task_id = task
        return task

    def _acme_submit_handover(self, category, dm):
        request = self.env['approval.request'].with_context(acme_pm_workflow=_PM_WORKFLOW).create({
            'name': _('PM Assignment: %s', self.display_name),
            'category_id': category.id,
            'acme_project_id': self.id,
            'request_owner_id': self.env.user.id,
            'acme_pm_dm_id': dm.id,
            'acme_pm_registration': self._acme_pm_registration_snapshot(),
        })
        request.approver_ids = [
            Command.clear(), Command.create({'user_id': dm.id, 'required': True}),
        ]
        return request


class PMAssignmentApproval(models.Model):
    _inherit = 'approval.request'

    acme_pm_dm_id = fields.Many2one('res.users', readonly=True, copy=False)
    acme_pm_registration = fields.Text(readonly=True, copy=False)
    acme_pm_handover_activity_id = fields.Many2one('mail.activity', readonly=True, copy=False, ondelete='set null')

    @api.model_create_multi
    def create(self, vals_list):
        requests = super().create(vals_list)
        if requests.filtered(lambda request: request.acme_kind == 'pm_assignment') and (
            self.env.context.get('acme_pm_workflow') is not _PM_WORKFLOW
        ):
            raise UserError(_('Submit PM Assignment from the verified project.'))
        return requests

    def write(self, vals):
        if self.env.context.get('acme_pm_workflow') is not _PM_WORKFLOW:
            protected = {'acme_pm_dm_id', 'acme_pm_registration', 'acme_pm_handover_activity_id', 'acme_project_id',
                         'category_id', 'approver_ids', 'request_owner_id'}
            if protected.intersection(vals) and (
                self.filtered(lambda request: request.acme_kind == 'pm_assignment')
                or ('category_id' in vals and self.env['approval.category'].browse(
                    vals['category_id'] or []
                ).acme_kind == 'pm_assignment')
            ):
                raise UserError(_('PM Assignment routing and registration are locked.'))
        return super().write(vals)

    def action_confirm(self):
        if self.filtered(lambda request: request.acme_kind == 'pm_assignment') and (
            self.env.context.get('acme_pm_workflow') is not _PM_WORKFLOW
        ):
            raise UserError(_('Submit PM Assignment from the verified project.'))
        return super().action_confirm()

    def _action_force_approval(self):
        if self.filtered(lambda request: request.acme_kind == 'pm_assignment'):
            raise UserError(_('Only the designated DM can approve a PM Assignment.'))
        return super()._action_force_approval()

    def action_approve(self, approver=None):
        assignments = self.filtered(lambda request: request.acme_kind == 'pm_assignment')
        for request in assignments:
            request.check_access('write')
            project = request.acme_project_id
            project._check_lifecycle_for_handover()
            is_admin = self.env.user.has_group('base.group_system')
            if not is_admin and (request.acme_pm_dm_id != self.env.user or not self.env.user.has_group(
                'acme_project_core.group_acme_dm'
            )):
                raise AccessError(_('Only the designated DM can approve this assignment.'))
            if request.request_status != 'pending' or request.approver_ids.user_id != self.env.user:
                raise UserError(_('This PM assignment is no longer awaiting your approval.'))
            submitted = json.loads(request.acme_pm_registration)
            current = json.loads(project._acme_pm_registration_snapshot())
            if 'contract_advance' not in submitted:
                current.pop('contract_advance')
            if submitted != current:
                raise UserError(_('Registration changed after submission. Submit a new PM assignment.'))
            if not project.acme_handover_task_id:
                raise UserError(_('The Handover task is missing.'))
        result = super(PMAssignmentApproval, self.with_context(
            acme_pm_workflow=_PM_WORKFLOW
        )).action_approve(approver=None if assignments else approver)
        for request in assignments.filtered(lambda record: record.request_status == 'approved'):
            project = request.acme_project_id
            project.with_context(acme_pm_workflow=_PM_WORKFLOW).user_id = project.acme_pm_candidate_id
            followers = project.company_id.acme_handover_partner_ids
            task = project.acme_handover_task_id
            project.message_subscribe(partner_ids=followers.ids)
            task.message_subscribe(partner_ids=followers.ids)
            task.state = '1_done'
            if request.acme_pm_handover_activity_id:
                request.acme_pm_handover_activity_id.action_feedback(feedback=_('DM approved the PM Assignment request.'))
            message = _('DM approved the registration, confirmed the Project Manager, and completed handover.')
            project.message_post(body=message)
            task.message_post(body=message)
        return result


class PMAssignmentApprover(models.Model):
    _inherit = 'approval.approver'

    @api.model_create_multi
    def create(self, vals_list):
        approvers = super().create(vals_list)
        approvers._acme_check_pm_routing()
        return approvers

    def write(self, vals):
        if {'user_id', 'required', 'sequence', 'request_id'}.intersection(vals) or vals.get('status') == 'approved':
            self._acme_check_pm_routing()
            if vals.get('request_id') and self.env['approval.request'].browse(
                vals['request_id']
            ).acme_kind == 'pm_assignment':
                raise UserError(_('PM Assignment routing is locked.'))
        return super().write(vals)

    def _acme_check_pm_routing(self):
        if self.env.context.get('acme_pm_workflow') is not _PM_WORKFLOW and self.filtered(
            lambda approver: approver.request_id.acme_kind == 'pm_assignment'
        ):
            raise UserError(_('Use the PM Assignment workflow to change routing or approve.'))

    @api.ondelete(at_uninstall=False)
    def _unlink_except_pm_routing(self):
        self._acme_check_pm_routing()
