from collections import defaultdict

from odoo import api, Command, fields, models, _
from odoo.exceptions import AccessError, UserError


class ProjectBoqAmendment(models.Model):
    _inherit = 'project.project'

    acme_pending_amendment_id = fields.Many2one('approval.request', compute='_compute_acme_pending_amendment')

    def _compute_acme_pending_amendment(self):
        approvals = self.env['approval.request']
        if not approvals.has_access('read'):
            self.acme_pending_amendment_id = False
            return
        for project in self:
            project.acme_pending_amendment_id = approvals.search([
                ('acme_project_id', '=', project.id), ('acme_kind', '=', 'variation'),
                ('request_status', '=', 'pending'),
            ], order='id desc').filtered(lambda request: (request.acme_variation_snapshot or {}).get('lines'))[:1]

    def action_acme_open_pending_amendment(self):
        self.ensure_one()
        self.check_access('read')
        self.invalidate_recordset(['acme_pending_amendment_id'])
        request = self.acme_pending_amendment_id
        if not request:
            raise UserError(_('There is no pending BOQ amendment.'))
        request.check_access('read')
        return {'type': 'ir.actions.act_window', 'res_model': request._name, 'res_id': request.id, 'view_mode': 'form'}

    def _acme_check_amendment_editor(self):
        self.ensure_one()
        self.check_access('write')
        if self.user_id != self.env.user:
            raise AccessError(_('Only the assigned Project Manager may amend the BOQ.'))
        self.invalidate_recordset(['acme_pending_amendment_id'])
        if (not self.acme_boq_initialized or not self.acme_boq_ids or self.acme_pending_amendment_id
                or self.acme_boq_state == 'pending'):
            raise UserError(_('An initialized BOQ without a pending amendment is required.'))
        if self.acme_boq_state == 'draft':
            self._acme_require_requester()

    def action_acme_open_boq_wizard(self):
        self._acme_check_amendment_editor()
        draft = self.acme_boq_state == 'draft'
        wizard = self.env['acme.boq.amendment.wizard'].create({
            'project_id': self.id, 'revision': self.acme_boq_revision,
            'reason': self.acme_boq_reason if draft else False,
            'line_ids': [Command.create({
                'boq_id': line.id, 'current_quantity': line.quantity,
                'proposed_quantity': line.proposed_quantity if draft else line.quantity,
            }) for line in self.acme_boq_ids],
        })
        return {'type': 'ir.actions.act_window', 'name': _('Edit BOQ'), 'res_model': wizard._name,
                'res_id': wizard.id, 'view_mode': 'form', 'target': 'new'}


class BoqAmendmentWizard(models.TransientModel):
    _name = 'acme.boq.amendment.wizard'
    _description = 'Edit Approved BOQ Quantities'

    project_id = fields.Many2one('project.project', required=True, readonly=True)
    revision = fields.Integer(readonly=True)
    reason = fields.Text()
    line_ids = fields.One2many('acme.boq.amendment.wizard.line', 'wizard_id')

    def action_submit(self):
        self.ensure_one()
        self.check_access('write')
        project = self.project_id
        project._acme_lock_boq()
        project._acme_check_amendment_editor()
        reason = (self.reason or '').strip()
        if not reason:
            raise UserError(_('Enter the reason for the BOQ amendment.'))
        if (project.acme_boq_revision != self.revision or self.line_ids.boq_id != project.acme_boq_ids
                or len(self.line_ids) != len(project.acme_boq_ids)
                or any(line.current_quantity != line.boq_id.quantity for line in self.line_ids)):
            raise UserError(_('The effective BOQ changed. Reopen Edit BOQ before submitting.'))
        changed = self.line_ids.filtered(lambda line: line.proposed_quantity != line.current_quantity)
        if not changed:
            raise UserError(_('There are no BOQ quantity changes to submit.'))
        if project.acme_boq_state == 'approved':
            project.action_acme_edit_boq()
        project.acme_boq_reason = reason
        for line in self.line_ids:
            if line.boq_id.proposed_quantity != line.proposed_quantity:
                line.boq_id.proposed_quantity = line.proposed_quantity
        project.action_acme_submit_boq()
        return {'type': 'ir.actions.act_window_close'}


class BoqAmendmentWizardLine(models.TransientModel):
    _name = 'acme.boq.amendment.wizard.line'
    _description = 'Proposed BOQ Quantity'

    wizard_id = fields.Many2one('acme.boq.amendment.wizard', required=True, ondelete='cascade')
    boq_id = fields.Many2one('acme.project.boq', string='BOQ Item', required=True, readonly=True)
    current_quantity = fields.Float(string='Current Approved Qty', readonly=True)
    proposed_quantity = fields.Float(string='Proposed Qty')


class BoqAmendmentHistory(models.Model):
    _inherit = 'acme.project.boq.revision'

    acme_quantity_changes = fields.Char(compute='_compute_acme_quantity_changes')
    acme_variation_request_id = fields.Many2one('approval.request', compute='_compute_acme_variation_request')

    @api.depends('changes')
    def _compute_acme_quantity_changes(self):
        for revision in self:
            revision.acme_quantity_changes = ', '.join(
                '%s: %g → %g' % (change['after'].get('name') or change['before'].get('name') or '',
                                 change['before'].get('quantity', 0), change['after'].get('quantity', 0))
                for change in (revision.changes or [])
                if change['before'].get('quantity', 0) != change['after'].get('quantity', 0)
            )

    def _compute_acme_variation_request(self):
        approvals = self.env['approval.request']
        if not approvals.has_access('read'):
            self.acme_variation_request_id = False
            return
        requests = approvals.search([('acme_variation_revision_id', 'in', self.ids)])
        by_revision = {request.acme_variation_revision_id.id: request for request in requests}
        for revision in self:
            revision.acme_variation_request_id = by_revision.get(revision.id, False)


class BoqVerifiedExecution(models.Model):
    _inherit = 'acme.project.boq'

    acme_verified_executed_qty = fields.Float(compute='_compute_acme_verified_execution')
    acme_verified_executed_amount = fields.Monetary(compute='_compute_acme_verified_execution', currency_field='currency_id')

    @api.depends('executed_quantity', 'unit_rate', 'currency_id')
    def _compute_acme_verified_execution(self):
        records = self.env['acme.project.measurement'].search([
            ('boq_id', 'in', self.ids), ('certificate_id.acme_ipc_managed', '=', True),
            ('certificate_id.state', 'in', ['draft', 'certified']), ('acme_site_quantity', '>', 0),
        ])
        measured = defaultdict(dict)
        delivered = defaultdict(dict)
        for line in records:
            certificate = line.certificate_id
            if certificate.acme_evidence_state != 'checked':
                continue
            if line.acme_billing_basis == 'delivery':
                move = line.acme_delivery_move_id
                if not move:
                    continue
                accepted, quantity = delivered[line.boq_id.id].get(move.id, (0, 0))
                delivered[line.boq_id.id][move.id] = (
                    max(accepted, line.acme_accepted_quantity), quantity + line.acme_site_quantity,
                )
            else:
                key = (certificate.acme_site, certificate.acme_period_start, certificate.acme_period_end)
                quantities = measured[line.boq_id.id]
                quantities[key] = max(quantities.get(key, 0), line.acme_site_quantity)
        for line in self:
            quantity = (sum(measured[line.id].values()) + sum(min(accepted, total)
                        for accepted, total in delivered[line.id].values())
                        if line.id in measured or line.id in delivered else line.executed_quantity)
            line.acme_verified_executed_qty = quantity
            amount = quantity * line.unit_rate
            line.acme_verified_executed_amount = line.currency_id.round(amount) if line.currency_id else amount
