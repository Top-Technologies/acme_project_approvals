from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


class ProjectApprovalLinks(models.Model):
    _inherit = 'project.project'

    acme_has_advance_request = fields.Boolean(compute='_compute_acme_request_slots')
    acme_has_bond_request = fields.Boolean(compute='_compute_acme_request_slots')

    @api.model
    def _acme_request_slot_kinds(self, kind):
        if kind == 'advance':
            return ('advance',)
        if kind in ('bond', 'guarantee'):
            return ('bond', 'guarantee')
        return ()

    def _acme_occupied_requests(self, kinds, excluded_id=0):
        self.env['approval.request'].flush_model(['acme_project_id', 'category_id', 'request_status'])
        self.env['approval.category'].flush_model(['acme_kind'])
        self.env.cr.execute('''
            SELECT DISTINCT r.acme_project_id FROM approval_request r
            JOIN approval_category c ON c.id = r.category_id
            WHERE r.acme_project_id IN %s AND c.acme_kind IN %s
              AND r.request_status IN ('new', 'pending', 'approved') AND r.id != %s
        ''', [tuple(self.ids) or (0,), kinds, excluded_id])
        return {row[0] for row in self.env.cr.fetchall()}

    def _compute_acme_request_slots(self):
        advance = self._acme_occupied_requests(('advance',))
        bond = self._acme_occupied_requests(('bond', 'guarantee'))
        for project in self:
            project.acme_has_advance_request = project.id in advance
            project.acme_has_bond_request = project.id in bond

    def _acme_check_request_slot(self, kind, excluded_id=0):
        kinds = self._acme_request_slot_kinds(kind)
        if not self or not kinds:
            return
        self.check_access('read')
        self.env.cr.execute('UPDATE project_project SET id = id WHERE id IN %s', [tuple(self.ids)])
        if self._acme_occupied_requests(kinds, excluded_id):
            raise UserError(_('An active request already exists. Open it from the project Requests button.'))

    acme_approval_count = fields.Integer(compute='_compute_approval_count')
    acme_pm_candidate_id = fields.Many2one('res.users', string='Proposed Project Manager', tracking=True, copy=False)
    acme_handover_task_id = fields.Many2one('project.task', string='Handover Task', readonly=True, copy=False)

    def _acme_check_boq_export(self):
        self.ensure_one()
        self.check_access('read')
        if not self.acme_boq_initialized:
            raise UserError(_('Initialize the signed BOQ before exporting.'))

    def action_acme_export_boq(self):
        self._acme_check_boq_export()
        return {'type': 'ir.actions.act_url', 'url': '/acme/project/%s/boq.xlsx' % self.id, 'target': 'download'}

    def _compute_approval_count(self):
        for project in self:
            project.acme_approval_count = self.env['approval.request'].search_count([
                ('acme_project_id', '=', project.id)
            ]) if self.env['approval.request'].has_access('read') else 0

    def action_acme_approvals(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Project Approvals'),
            'res_model': 'approval.request',
            'view_mode': 'list,form',
            'domain': [('acme_project_id', '=', self.id)],
            'context': {'default_acme_project_id': self.id},
        }

    def _acme_request(self, kind):
        self.ensure_one()
        self.check_access('read')
        self._acme_check_request_slot(kind)
        category_model = self.env['approval.category']
        domain = [
            ('acme_kind', '=', kind),
            ('active', '=', True),
            ('company_id', '=', self.company_id.id),
        ]
        categories = category_model.search(domain)
        if not categories and not category_model._fields['company_id'].required:
            categories = category_model.search([
                ('acme_kind', '=', kind),
                ('active', '=', True),
                ('company_id', '=', False),
            ])
        kind_label = dict(category_model._fields['acme_kind'].selection).get(kind, kind)
        if not categories:
            # Fallback auto-creation of missing category
            category = category_model.create({
                'name': f'Project {kind_label}',
                'acme_kind': kind,
                'company_id': self.company_id.id,
                'approval_minimum': 1 if kind == 'bond' else 2,
                'approver_sequence': kind in ('advance', 'guarantee'),
            })
            categories = category
        elif len(categories) > 1:
            category = categories[0]
        else:
            category = categories

        default_amount = self.acme_performance_bond_amount if kind in ('bond', 'guarantee') else 0
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'approval.request',
            'view_mode': 'form',
            'context': {
                'default_category_id': category.id,
                'default_acme_project_id': self.id,
                'default_name': '%s — %s' % (category.name, self.name),
                'default_partner_id': self.partner_id.id,
                'default_company_id': self.company_id.id,
                'default_acme_amount': default_amount,
                'default_acme_contract_reference': self.acme_contract_reference,
            },
        }

    def action_acme_bond_request(self):
        return self._acme_request('bond')

    def action_acme_advance_request(self):
        action = self._acme_request('advance')
        defaults = self.env['approval.request']._advance_project_defaults(self)
        defaults.update(
            acme_advance_contract_name=self.name,
            acme_advance_description=_('Advance payment for %s', self.name),
        )
        action['context'].update({f'default_{key}': value for key, value in defaults.items()})
        return action

    def action_acme_guarantee_request(self):
        return self._acme_request('guarantee')

    def _check_lifecycle_for_handover(self):
        for project in self:
            if project.acme_lifecycle_state != 'verified':
                raise UserError(_('Project must be verified by EDM before handover or PM assignment (current state: %s).') % (project.acme_lifecycle_state or 'draft'))

    def _check_active_guarantees(self):
        for project in self:
            active = project.acme_guarantee_ids.filtered(lambda g: g.state == 'active')
            if not active:
                raise UserError(_('Project %s does not have an active bank guarantee.') % project.name)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('acme_project_sequence'):
                vals['acme_project_sequence'] = self.env['ir.sequence'].next_by_code('acme.project.code') or '/'
            company_id = vals.get('company_id')
            for f_key in ('documents_folder_id', 'acme_documents_folder_id'):
                f_id = vals.get(f_key)
                if f_id and company_id:
                    folder = self.env['documents.document'].browse(f_id)
                    if folder.exists() and folder.company_id and folder.company_id.id != company_id:
                        vals[f_key] = False
        projects = super().create(vals_list)
        for project in projects:
            if not project.account_id:
                plan = self.env['account.analytic.plan'].search([], limit=1)
                if not plan:
                    plan = self.env['account.analytic.plan'].create({
                        'name': 'Projects Plan',
                    })
                analytic = self.env['account.analytic.account'].create({
                    'name': project.name,
                    'code': project.acme_project_sequence or False,
                    'company_id': project.company_id.id,
                    'partner_id': project.partner_id.id,
                    'plan_id': plan.id,
                })
                project.account_id = analytic
        return projects


    def write(self, vals):
        if 'acme_bond_basis' in vals and self.env['approval.request'].search_count([
            ('acme_project_id', 'in', self.ids),
            ('category_id.acme_kind', '=', 'bond'),
            ('request_status', 'in', ['pending', 'approved']),
        ]):
            raise UserError(_('The bond basis cannot change while a bond request is pending or approved.'))

        if vals.get('acme_pm_candidate_id'):
            self._check_lifecycle_for_handover()

        return super().write(vals)

    def copy(self, default=None):
        if default and 'company_id' in default:
            res = super(ProjectApprovalLinks, self.with_context(no_create_folder=True)).copy(default)
            for old_project, new_project in zip(self, res):
                if old_project.documents_folder_id:
                    new_project.documents_folder_id = old_project.documents_folder_id.sudo().copy({
                        'name': new_project.name,
                        'owner_id': False,
                        'company_id': new_project.company_id.id,
                    })
            return res
        return super().copy(default)


class GuaranteeApprovalLink(models.Model):
    _inherit = 'acme.project.guarantee'

    approval_id = fields.Many2one('approval.request', string='Approved Request', ondelete='restrict', check_company=True)

    acme_approved_addressee = fields.Char(related='approval_id.acme_addressee')
    acme_approved_addressee_address = fields.Text(related='approval_id.acme_addressee_address')
    acme_approved_beneficiary = fields.Char(related='approval_id.acme_beneficiary')
    acme_approved_account_number = fields.Char(related='approval_id.acme_account_number')
    acme_approved_branch = fields.Char(related='approval_id.acme_branch')
    acme_approved_city = fields.Char(related='approval_id.acme_city')

    def write(self, vals):
        if {'approval_id', 'project_id', 'bank_id', 'amount', 'guarantee_type'}.intersection(vals) and self.filtered('approval_id'):
            raise UserError(_('Approved guarantee details are maintained by the approval request.'))
        return super().write(vals)

    @api.constrains('approval_id', 'project_id')
    def _check_approval(self):
        for guarantee in self:
            if guarantee.approval_id and (guarantee.approval_id.acme_project_id != guarantee.project_id or guarantee.approval_id.request_status != 'approved'):
                raise ValidationError(_('Select an approved request for this project.'))
