import math

from odoo import api, Command, fields, models, _
from odoo.exceptions import AccessError, UserError
from odoo.addons.acme_project_core.models.certificate import _CERTIFY_TOKEN

from .ipc import _DOCUMENTS, _IPC_TOKEN, _LINE_EVIDENCE, _ROUND_EVIDENCE
from .progress_certificate import _PC_TOKEN


_BILL_TOKEN = object()
_BILL_AUDIT = {'acme_bill_pm_id', 'acme_bill_submitted_at', 'acme_bill_dm_id',
               'acme_bill_approved_at', 'acme_bill_signature', 'acme_source_bill_id'}
_EVIDENCE_FIELDS = (
    'acme_site_quantity', 'acme_billing_basis', 'acme_delivery_move_id',
    'acme_billing_milestone_id', 'acme_accepted_quantity', 'acme_line_takeoff',
    'acme_inspection_evidence', *_DOCUMENTS,
)


class ProjectRequestBill(models.Model):
    _inherit = 'project.project'

    def _compute_counts(self):
        super()._compute_counts()
        if self.env['acme.project.certificate'].has_access('read'):
            for project in self:
                project.acme_certificate_count = self.env['acme.project.certificate'].search_count([
                    ('project_id', '=', project.id), ('acme_certificate_type', '!=', 'request_bill'),
                ])

    def action_acme_certificates(self):
        action = super().action_acme_certificates()
        action['domain'] = [*action['domain'], ('acme_certificate_type', '!=', 'request_bill')]
        return action

    def action_acme_request_bill(self):
        self.ensure_one()
        self._acme_require_site_editor()
        self.env.cr.execute('UPDATE project_project SET id = id WHERE id = %s', [self.id])
        self.env.invalidate_all()
        certificates = self.env['acme.project.certificate']
        drafts = certificates.search([
            ('project_id', '=', self.id), ('acme_certificate_type', '=', 'request_bill'),
            ('state', '=', 'draft'),
        ], order='number desc')
        bill = drafts.filtered(lambda row: not row.acme_financial_ipc_ids
                               or row.acme_financial_ipc_ids.state == 'draft')[:1]
        if not bill:
            if self.user_id != self.env.user:
                raise AccessError(_('Only the assigned PM may open a Request Bill round.'))
            if certificates.search_count([('project_id', '=', self.id),
                    ('acme_certificate_type', '=', 'progress'), ('state', '=', 'draft')], limit=1):
                raise UserError(_('Finish the existing draft IPC from Certificates before starting another round.'))
            if not self.acme_boq_initialized or not self.acme_boq_ids:
                raise UserError(_('Finalize the approved project BOQ before opening a Request Bill.'))
            prior = certificates.search([('project_id', '=', self.id), ('number', '>', 0)],
                                        order='number desc', limit=1)
            bill = certificates.with_context(acme_bill_token=_BILL_TOKEN).create({
                'project_id': self.id, 'acme_certificate_type': 'request_bill',
                'line_ids': [Command.create({'boq_id': line.id, 'acme_site_quantity': 0})
                             for line in self.acme_boq_ids],
            })
            bill.with_context(acme_certify_token=_CERTIFY_TOKEN).number = prior.number + 1
        if self.user_id == self.env.user and bill.acme_bill_status == 'draft':
            bill._acme_refresh_pc_previous_lines()
        return {'type': 'ir.actions.act_window', 'res_model': bill._name, 'res_id': bill.id,
                'name': _('Request Bill'), 'views': [(self.env.ref(
                    'acme_project_approvals.request_bill_form').id, 'form')]}


class RequestBillCertificate(models.Model):
    _inherit = 'acme.project.certificate'

    acme_certificate_type = fields.Selection(selection_add=[('request_bill', 'Round')],
                                              ondelete={'request_bill': 'set default'})
    acme_source_bill_id = fields.Many2one('acme.project.certificate', copy=False, readonly=True,
                                         check_company=True, ondelete='restrict')
    acme_bill_evidence_line_ids = fields.One2many('acme.project.measurement', 'certificate_id')
    acme_financial_ipc_ids = fields.One2many('acme.project.certificate', 'acme_source_bill_id')
    acme_bill_pm_id = fields.Many2one('res.users', readonly=True, copy=False)
    acme_bill_submitted_at = fields.Datetime(readonly=True, copy=False)
    acme_bill_dm_id = fields.Many2one('res.users', readonly=True, copy=False)
    acme_bill_approved_at = fields.Datetime(readonly=True, copy=False)
    acme_bill_signature = fields.Json(readonly=True, copy=False)
    acme_bill_status = fields.Selection([('draft', 'Draft'), ('pending', 'Pending DM'),
                                       ('approved', 'DM Approved'), ('linked', 'IPC Created')],
                                      compute='_compute_bill_status')
    _acme_unique_source_bill = models.Constraint('UNIQUE(acme_source_bill_id)', 'A Request Bill can have only one IPC.')

    @api.depends('number', 'project_id.name', 'acme_certificate_type')
    def _compute_name(self):
        super()._compute_name()
        for bill in self.filtered(lambda record: record.acme_certificate_type == 'request_bill'):
            bill.name = _('%(project)s / Round %(number)s', project=bill.project_id.name, number=bill.number)

    @api.depends('acme_bill_signature', 'acme_bill_dm_id', 'acme_financial_ipc_ids.acme_verified_evidence')
    def _compute_bill_status(self):
        for bill in self:
            bill.acme_bill_status = ('linked' if bill.acme_financial_ipc_ids.acme_verified_evidence else 'approved' if bill.acme_bill_dm_id
                                    else 'pending' if bill.acme_bill_signature else 'draft')

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get('acme_bill_token') is not _BILL_TOKEN:
            if any('default_' + key in self.env.context for key in _BILL_AUDIT):
                raise AccessError(_('Use the Request Bill workflow to set approval and IPC links.'))
            for vals in vals_list:
                kind = vals.get('acme_certificate_type', self.env.context.get('default_acme_certificate_type', 'progress'))
                if kind in ('request_bill', 'progress') or _BILL_AUDIT.intersection(vals):
                    raise AccessError(_('Start a round from Project → Request Bill.'))
        return super().create(vals_list)

    def write(self, vals):
        internal = self.env.context.get('acme_bill_token') is _BILL_TOKEN
        if not internal and _BILL_AUDIT.intersection(vals):
            raise AccessError(_('Use the Request Bill approval actions.'))
        bills = self.filtered(lambda row: row.acme_certificate_type == 'request_bill')
        linked = self.filtered('acme_source_bill_id')
        if not internal and ('acme_certificate_type' in vals and (
                bills or linked or vals['acme_certificate_type'] in ('request_bill', 'progress'))
                or bills and 'project_id' in vals):
            raise AccessError(_('A Request Bill must stay on its original project.'))
        if not internal and (set(vals) & _ROUND_EVIDENCE or 'line_ids' in vals):
            bills._acme_bill_edit()
            if linked and set(vals) & _ROUND_EVIDENCE:
                raise UserError(_('The IPC uses the evidence approved on its Request Bill.'))
        result = super().write(vals)
        if not internal and (set(vals) & _ROUND_EVIDENCE or 'line_ids' in vals):
            bills._acme_bill_reset()
        return result

    @api.ondelete(at_uninstall=False)
    def _unlink_except_bill_round(self):
        if self.filtered(lambda row: row.acme_certificate_type == 'request_bill'):
            raise UserError(_('Keep issued Request Bill round numbers in the project history.'))

    def _acme_bill_edit(self):
        for bill in self:
            bill.check_access('write')
            bill.project_id._acme_require_site_editor()
            if bill.state != 'draft' or (bill.acme_financial_ipc_ids and (
                    bill.acme_financial_ipc_ids.state != 'draft' or bill.acme_financial_ipc_ids.acme_verified_evidence)):
                raise UserError(_('The Request Bill quantities and evidence are frozen after IPC creation.'))

    def _acme_bill_reset(self):
        changed = self.filtered('acme_bill_signature')
        if changed:
            changed.with_context(acme_bill_token=_BILL_TOKEN).write({
                'acme_bill_signature': False, 'acme_bill_pm_id': False, 'acme_bill_submitted_at': False,
                'acme_bill_dm_id': False, 'acme_bill_approved_at': False,
            })

    def _acme_bill_lock(self):
        self.ensure_one()
        self.check_access('write')
        if self.acme_certificate_type != 'request_bill' or self.state != 'draft':
            raise UserError(_('Open a current Request Bill.'))
        self.env.cr.execute('UPDATE project_project SET id = id WHERE id = %s', [self.project_id.id])
        self.env.invalidate_all()

    def action_acme_submit_bill(self):
        self._acme_bill_lock()
        self._acme_pc_require_pm()
        self._acme_bill_edit()
        if self.acme_bill_status != 'draft':
            raise UserError(_('This Request Bill is already submitted.'))
        if not self.signed_takeoff or not self.acme_period_start or not self.acme_period_end or self.acme_period_start > self.acme_period_end:
            raise UserError(_('Attach a signed Take-off Sheet and enter a valid period.'))
        self._acme_refresh_pc_previous_lines()
        lines = self.line_ids.filtered('acme_site_quantity')
        if not lines:
            raise UserError(_('Enter executed quantities for this round.'))
        for line in lines:
            line._acme_check_ipc_evidence()
        self.with_context(acme_bill_token=_BILL_TOKEN).write({
            'acme_bill_signature': self._acme_evidence_signature(),
            'acme_bill_pm_id': self.env.uid, 'acme_bill_submitted_at': fields.Datetime.now(),
        })

    def action_acme_approve_bill(self):
        self._acme_bill_lock()
        dm = self.env['acme.approval.position.assignment'].for_position(self.company_id, 'dm').user_id
        if dm != self.env.user or dm == self.acme_bill_pm_id or not dm.has_group('acme_project_core.group_acme_dm'):
            raise AccessError(_('Only the designated independent DM can approve this Request Bill.'))
        if self.acme_bill_status != 'pending' or self.acme_bill_signature != self._acme_evidence_signature():
            raise UserError(_('The PM must submit the current quantities and evidence for review.'))
        self.with_context(acme_bill_token=_BILL_TOKEN).write({
            'acme_bill_dm_id': dm.id, 'acme_bill_approved_at': fields.Datetime.now(),
        })

    def _acme_bill_copy_fields(self, line):
        return {'quantity': line.acme_site_quantity, **{
            key: line[key].id if line._fields[key].relational else line[key] for key in _EVIDENCE_FIELDS
        }}

    def action_acme_return_to_request_bill(self):
        self._acme_pc_lock()
        self._acme_pc_require_pm()
        bill = self.acme_source_bill_id
        if not bill or self.state != 'draft' or (self.acme_pc_invoice_ids and self.acme_pc_invoice_ids.state != 'draft'):
            raise UserError(_('Return an unposted financial IPC draft to its Request Bill.'))
        self.with_context(acme_ipc_token=_IPC_TOKEN).write({'acme_verified_evidence': False})
        self._acme_ipc_invalidate_reviews()
        self.acme_pc_invoice_ids._acme_clear_invoice_signoffs()
        bill._acme_bill_reset()
        return self.project_id.action_acme_request_bill()

    def action_acme_create_linked_ipc(self):
        self._acme_bill_lock()
        self._acme_pc_require_pm()
        ipc = self.acme_financial_ipc_ids
        if not ipc or not ipc.acme_verified_evidence:
            if self.acme_bill_status != 'approved' or self.acme_bill_signature != self._acme_evidence_signature():
                raise UserError(_('DM must approve the current Request Bill before IPC creation.'))
            bill = self.with_context(bin_size=False)
            values = {
                'signed_takeoff': bill.signed_takeoff, 'signed_takeoff_filename': bill.signed_takeoff_filename,
                'acme_round_evidence_ids': [Command.set(bill.acme_round_evidence_ids.ids)],
                'acme_site': bill.acme_site, 'acme_period_start': bill.acme_period_start,
                'acme_period_end': bill.acme_period_end,
            }
            if ipc:
                ipc.with_context(acme_bill_token=_BILL_TOKEN).write(values)
                approved = {line.boq_id.id: line for line in bill.line_ids}
                for line in ipc.line_ids.with_context(acme_bill_token=_BILL_TOKEN):
                    line.write(bill._acme_bill_copy_fields(approved[line.boq_id.id]))
            else:
                ipc = self.with_context(acme_bill_token=_BILL_TOKEN).create({
                    'project_id': self.project_id.id, 'acme_certificate_type': 'progress',
                    'acme_source_bill_id': self.id, **values,
                    'line_ids': [Command.create({'boq_id': line.boq_id.id, **bill._acme_bill_copy_fields(line)})
                                 for line in bill.line_ids],
                })
            ipc.with_context(acme_ipc_token=_IPC_TOKEN).write({
                'acme_verified_evidence': ipc._acme_evidence_signature(),
                'acme_evidence_verified_by_id': self.acme_bill_dm_id.id,
                'acme_evidence_verified_at': self.acme_bill_approved_at,
            })
            ipc._acme_refresh_pc_previous_lines()
        return {'type': 'ir.actions.act_window', 'res_model': ipc._name, 'res_id': ipc.id,
                'view_mode': 'form', 'name': _('Financial IPC')}

    def action_cancel(self):
        if self.filtered(lambda record: record.acme_certificate_type == 'request_bill'):
            self.filtered(lambda record: record.acme_certificate_type == 'request_bill')._acme_bill_edit()
        if self.filtered('acme_source_bill_id'):
            raise UserError(_('Use the approved IPC revision workflow to correct a linked round.'))
        return super().action_cancel()

    def action_certify(self):
        if self.filtered(lambda record: record.acme_certificate_type == 'request_bill'):
            raise UserError(_('Create the financial IPC from the approved Request Bill.'))
        return super().action_certify()

    def action_acme_pc_submit(self):
        result = super().action_acme_pc_submit()
        bill = self.acme_source_bill_id
        if bill:
            if not bill.acme_bill_dm_id or bill.acme_bill_signature != bill._acme_evidence_signature():
                raise UserError(_('DM must approve the linked Request Bill evidence.'))
            if self.acme_pc_route['dm'] != bill.acme_bill_dm_id.id:
                raise UserError(_('The assigned DM changed. Resolve ownership of the approved Request Bill.'))
            self.with_context(acme_pc_token=_PC_TOKEN).write({
                'acme_pc_dm_id': bill.acme_bill_dm_id.id, 'acme_pc_dm_at': bill.acme_bill_approved_at,
            })
        return result

    def action_acme_pc_dm_review(self):
        if self.acme_source_bill_id:
            raise UserError(_('DM review is recorded on the linked Request Bill.'))
        return super().action_acme_pc_dm_review()

    def _acme_pc_fingerprint(self, invoice):
        data = super()._acme_pc_fingerprint(invoice)
        if self.acme_source_bill_id:
            data['adjustments'] = [[line.id, line.acme_ipc_adjustment_reason or ''] for line in self.line_ids.sorted('id')]
        return data


class RequestBillMeasurement(models.Model):
    _inherit = 'acme.project.measurement'

    acme_ipc_adjustment_reason = fields.Char(string='Current Quantity Adjustment Reason', copy=False)

    acme_bill_to_date_qty = fields.Float(compute='_compute_acme_bill_execution')
    acme_bill_current_amount = fields.Monetary(compute='_compute_acme_bill_execution')
    acme_bill_to_date_amount = fields.Monetary(compute='_compute_acme_bill_execution')

    @api.depends('previous_quantity', 'acme_site_quantity', 'unit_rate', 'currency_id')
    def _compute_acme_bill_execution(self):
        for line in self:
            total = line.previous_quantity + line.acme_site_quantity
            line.acme_bill_to_date_qty = total
            amounts = (line.acme_site_quantity * line.unit_rate, total * line.unit_rate)
            if line.currency_id:
                amounts = tuple(map(line.currency_id.round, amounts))
            line.acme_bill_current_amount, line.acme_bill_to_date_amount = amounts

    def _acme_pc_history(self, delivery=None, site=None):
        self.ensure_one()
        if self.certificate_id.acme_certificate_type == 'request_bill':
            site = None
        return super()._acme_pc_history(delivery=delivery, site=site)

    def write(self, vals):
        bills = self.certificate_id.filtered(lambda row: row.acme_certificate_type == 'request_bill')
        bills._acme_bill_edit()
        if bills and 'acme_site_quantity' in vals:
            for bill in bills:
                bill._acme_pc_require_pm()
                if bill.acme_bill_status != 'draft':
                    raise UserError(_('Edit Current Executed Qty while the Request Bill is in draft.'))
            if not math.isfinite(vals['acme_site_quantity']) or vals['acme_site_quantity'] < 0:
                raise UserError(_('Enter a finite nonnegative Current Executed Qty.'))
        if bills and 'boq_id' in vals:
            raise UserError(_('Request Bill lines come from the approved project BOQ.'))
        if bills and 'quantity' in vals:
            raise UserError(_('Enter the site executed quantity on the Request Bill.'))
        if bills and {'acme_delivery_move_id', 'acme_billing_milestone_id'}.intersection(vals):
            bill_lines = self.filtered(lambda line: line.certificate_id in bills)
            for line in bill_lines:
                delivery = vals.get('acme_delivery_move_id', line.acme_delivery_move_id.id)
                milestone = vals.get('acme_billing_milestone_id', line.acme_billing_milestone_id.id)
                if delivery and milestone:
                    raise UserError(_('Use either an accepted delivery or a reached milestone for this line.'))
                basis = 'delivery' if delivery else 'milestone' if milestone else 'measured'
                super(RequestBillMeasurement, line).write(dict(vals, acme_billing_basis=basis))
            (self - bill_lines).write(vals)
            bills._acme_bill_reset()
            return True

        linked = self.filtered('certificate_id.acme_source_bill_id')
        internal = self.env.context.get('acme_bill_token') is _BILL_TOKEN
        if linked and not internal and _LINE_EVIDENCE.intersection(vals):
            raise UserError(_('Change site evidence through the Request Bill workflow.'))
        if not internal and 'quantity' in vals:
            for line in linked:
                quantity = vals['quantity']
                if not math.isfinite(quantity) or not 0 <= quantity <= line.acme_site_quantity:
                    raise UserError(_('The adjusted quantity must be within the approved site quantity.'))
                if quantity != line.quantity and not (vals.get('acme_ipc_adjustment_reason', line.acme_ipc_adjustment_reason) or '').strip():
                    raise UserError(_('Record a reason for the adjustment on this BOQ line.'))
        result = super().write(vals)
        if _LINE_EVIDENCE.intersection(vals):
            bills._acme_bill_reset()
        if 'acme_ipc_adjustment_reason' in vals:
            linked.certificate_id._acme_ipc_invalidate_reviews()
        return result

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            certificate = self.env['acme.project.certificate'].browse(
                vals.get('certificate_id') or self.env.context.get('default_certificate_id') or [])
            if certificate.acme_certificate_type == 'request_bill':
                if self.env.context.get('acme_bill_token') is not _BILL_TOKEN:
                    raise UserError(_('The Request Bill loads its lines from the approved BOQ.'))
                certificate._acme_bill_edit()
                if vals.get('quantity'):
                    raise UserError(_('Enter the site executed quantity on the Request Bill.'))
            if certificate.acme_source_bill_id and self.env.context.get('acme_bill_token') is not _BILL_TOKEN:
                raise UserError(_('The IPC lines come from its approved Request Bill.'))
        return super().create(vals_list)

    def unlink(self):
        if self.certificate_id.filtered(lambda row: row.acme_certificate_type == 'request_bill'):
            raise UserError(_('Keep the BOQ lines and leave unused Request Bill quantities at zero.'))
        if self.filtered('certificate_id.acme_source_bill_id'):
            raise UserError(_('Adjust current quantities instead of removing approved Request Bill lines.'))
        return super().unlink()
