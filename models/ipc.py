import hashlib
import math

from odoo import api, Command, fields, models, _
from odoo.exceptions import AccessError, UserError
from odoo.addons.acme_project_core.models.certificate import _CERTIFY_TOKEN
from .progress_certificate import _PC_TOKEN


_IPC_TOKEN = object()
_IPC_AUDIT = {'acme_ipc_managed', 'acme_evidence_verified_by_id', 'acme_evidence_verified_at',
              'acme_verified_evidence', 'acme_ipc_output_snapshot'}
_DOCUMENTS = ('acme_grn', 'acme_storekeeper_certificate', 'acme_delivery_note', 'acme_client_acceptance')
_LINE_EVIDENCE = {'acme_site_quantity', 'acme_line_takeoff', 'acme_accepted_quantity',
                  'acme_delivery_move_id', 'acme_inspection_evidence', 'acme_billing_basis',
                  'acme_billing_milestone_id', 'boq_id', *_DOCUMENTS}
_ROUND_EVIDENCE = {'signed_takeoff', 'signed_takeoff_filename', 'acme_round_evidence_ids',
                   'acme_period_start', 'acme_period_end', 'acme_site'}
_ROUND = [('acme_certificate_type', '=', 'progress')]


class ProjectIPC(models.Model):
    _inherit = 'project.project'

    acme_site_engineer_id = fields.Many2one(
        'res.users', string='Assigned Site Engineer', tracking=True,
        domain="[('share', '=', False), ('company_ids', 'in', [company_id])]",
    )

    @api.constrains('acme_site_engineer_id', 'company_id')
    def _check_acme_site_engineer(self):
        for project in self.filtered('acme_site_engineer_id'):
            user = project.acme_site_engineer_id
            if user.share or project.company_id not in user.company_ids:
                raise UserError(_('Assign an internal Site Engineer with access to the project company.'))

    def write(self, vals):
        if 'acme_site_engineer_id' in vals and any(project.user_id != self.env.user for project in self):
            if not self.env.user.has_group('project.group_project_manager'):
                raise AccessError(_('Only the assigned PM or Project Administrator may assign the Site Engineer.'))
        return super().write(vals)

    def _acme_require_site_editor(self):
        self.check_access('read')
        for project in self:
            if (project.company_id not in self.env.companies
                    or self.env.user not in (project.user_id | project.acme_site_engineer_id)):
                raise AccessError(_('Only the assigned Site Engineer or PM may enter execution and evidence for this project.'))

    def action_acme_request_bill(self):
        self.ensure_one()
        self._acme_require_site_editor()
        self.env.cr.execute('UPDATE project_project SET id = id WHERE id = %s', [self.id])
        self.env.invalidate_all()
        certificates = self.env['acme.project.certificate']
        drafts = certificates.search([('project_id', '=', self.id), ('state', '=', 'draft'), *_ROUND])
        if len(drafts) > 1:
            raise UserError(_('Resolve the existing overlapping IPC drafts before requesting this bill.'))
        certificate = drafts or certificates.create({'project_id': self.id, 'acme_certificate_type': 'progress'})
        if not certificate.line_ids and certificate.acme_ipc_managed:
            certificate.write({'line_ids': [Command.create({'boq_id': line.id, 'quantity': 0})
                                            for line in self.acme_boq_ids]})
        if certificate.acme_ipc_managed and self.env.user == self.user_id:
            certificate._acme_refresh_pc_previous_lines()
        return {'type': 'ir.actions.act_window', 'res_model': 'acme.project.certificate',
                'name': _('Request Bill'), 'view_mode': 'form', 'res_id': certificate.id}


class IPCCertificate(models.Model):
    _inherit = 'acme.project.certificate'

    acme_ipc_managed = fields.Boolean(readonly=True, copy=False)
    acme_ipc_is_pm = fields.Boolean(compute='_compute_ipc_is_pm')
    acme_ipc_round = fields.Char(compute='_compute_ipc_round')
    acme_round_evidence_ids = fields.Many2many('ir.attachment', 'acme_ipc_round_attachment_rel',
                                              'certificate_id', 'attachment_id', string='Round Evidence', copy=False)
    acme_evidence_verified_by_id = fields.Many2one('res.users', string='Evidence Checked By', readonly=True, copy=False)
    acme_evidence_verified_at = fields.Datetime(string='Evidence Checked On', readonly=True, copy=False)
    acme_verified_evidence = fields.Json(readonly=True, copy=False)
    acme_evidence_state = fields.Selection([('check', 'To be checked'), ('checked', 'Checked')],
                                           compute='_compute_acme_evidence_state', string='Take-off Evidence')
    acme_ipc_output_snapshot = fields.Json(readonly=True, copy=False)

    @api.depends('project_id.user_id')
    @api.depends_context('uid')
    def _compute_ipc_is_pm(self):
        for certificate in self:
            certificate.acme_ipc_is_pm = certificate.project_id.user_id == self.env.user

    def _acme_ipc_invalidate_reviews(self):
        drafts = self.filtered(lambda record: record.acme_ipc_managed and record.state == 'draft'
                               and record.acme_pc_submitted_at)
        if drafts:
            drafts.with_context(acme_pc_token=_PC_TOKEN).write({
                'acme_pc_route': False, 'acme_pc_snapshot': False, 'acme_pc_pm_id': False,
                'acme_pc_submitted_at': False, 'acme_pc_dm_id': False, 'acme_pc_dm_at': False,
            })

    @api.depends('number', 'project_id.name', 'acme_ipc_managed')
    def _compute_name(self):
        super()._compute_name()
        for certificate in self.filtered('acme_ipc_managed'):
            certificate.name = '%s / %s' % (certificate.project_id.name or '', certificate.acme_ipc_round)

    @api.depends('number')
    def _compute_ipc_round(self):
        for certificate in self:
            certificate.acme_ipc_round = _('Round %s', certificate.number) if certificate.number else _('Draft Round')

    @api.depends('acme_verified_evidence', 'signed_takeoff', 'line_ids.write_date',
                 'acme_round_evidence_ids.checksum', 'acme_round_evidence_ids.name')
    def _compute_acme_evidence_state(self):
        for certificate in self:
            certificate.acme_evidence_state = (
                'checked' if certificate.acme_verified_evidence
                and certificate.acme_verified_evidence == certificate._acme_evidence_signature() else 'check'
            )

    @api.model_create_multi
    def create(self, vals_list):
        if any(_IPC_AUDIT.intersection(vals) for vals in vals_list) or any(
            'default_' + key in self.env.context for key in _IPC_AUDIT
        ):
            raise AccessError(_('IPC evidence and output snapshots are recorded by workflow actions.'))
        rounds = [vals for vals in vals_list if vals.get(
            'acme_certificate_type', self.env.context.get('default_acme_certificate_type', 'progress')
        ) == 'progress']
        if not rounds:
            return super().create(vals_list)
        projects = self.env['project.project'].browse([
            vals.get('project_id') or self.env.context.get('default_project_id') for vals in rounds
        ]).exists()
        projects._acme_require_site_editor()
        if any(project.user_id != self.env.user for project in projects):
            raise AccessError(_('Only the assigned PM may open a new IPC round.'))
        for project in projects.sorted('id'):
            self.env.cr.execute('UPDATE project_project SET id = id WHERE id = %s', [project.id])
        self.env.invalidate_all()
        covered = set()
        for vals in rounds:
            project = vals.get('project_id') or self.env.context.get('default_project_id')
            if not project or project in covered or self.search_count([
                ('project_id', '=', project), ('state', '=', 'draft'), *_ROUND,
            ], limit=1):
                raise UserError(_('Open the existing IPC draft for this project.'))
            covered.add(project)
            vals['project_id'] = project
            vals['acme_ipc_managed'] = True
        certificates = super().create(vals_list)
        for certificate in certificates.filtered('acme_ipc_managed'):
            prior = self.search([('project_id', '=', certificate.project_id.id), ('number', '>', 0), *_ROUND],
                                order='number desc', limit=1)
            certificate.with_context(acme_certify_token=_CERTIFY_TOKEN).number = (
                certificate.acme_source_bill_id.number or prior.number + 1
            )
        return certificates

    def write(self, vals):
        if _IPC_AUDIT.intersection(vals) and self.env.context.get('acme_ipc_token') is not _IPC_TOKEN:
            raise AccessError(_('Record IPC evidence decisions using the workflow.'))
        certificates = self.filtered(lambda record: record.acme_certificate_type == 'progress')
        if _ROUND_EVIDENCE.intersection(vals) and certificates:
            certificates.project_id._acme_require_site_editor()
            if any(record.state != 'draft' for record in certificates):
                raise UserError(_('Reopen the unposted IPC before changing its evidence.'))
        if {'project_id', 'acme_certificate_type'}.intersection(vals) and self.filtered('acme_ipc_managed'):
            raise UserError(_('Keep a numbered IPC on its original project.'))
        result = super().write(vals)
        if _ROUND_EVIDENCE.intersection(vals) or 'line_ids' in vals:
            certificates._acme_ipc_invalidate_reviews()
        return result

    @api.ondelete(at_uninstall=False)
    def _unlink_except_numbered_ipc(self):
        if self.filtered('acme_ipc_managed'):
            raise UserError(_('Cancel numbered IPC rounds to keep their history.'))

    def _acme_evidence_signature(self):
        self.ensure_one()
        record = self.with_context(bin_size=False)
        def digest(binary):
            return hashlib.sha256(binary).hexdigest() if binary else False
        return {
            'period': [str(record.acme_period_start), str(record.acme_period_end), record.acme_site or ''],
            'takeoff': [digest(record.signed_takeoff), record.signed_takeoff_filename or ''],
            'attachments': [[item.id, item.name, item.checksum] for item in record.acme_round_evidence_ids.sorted('id')],
            'lines': [[line.id, line.boq_id.id, line.acme_site_quantity, line.acme_billing_basis,
                       line.acme_delivery_move_id.id, line.acme_billing_milestone_id.id, line.acme_accepted_quantity,
                       *[digest(line[key]) for key in ('acme_line_takeoff', 'acme_inspection_evidence', *_DOCUMENTS)]]
                      for line in record.line_ids.sorted('id')],
        }

    def action_acme_verify_ipc_evidence(self):
        self._acme_pc_lock()
        self._acme_pc_require_pm()
        if self.state != 'draft' or not self.acme_ipc_managed:
            raise UserError(_('Verify evidence on a draft IPC round.'))
        if not self.acme_period_start or not self.acme_period_end or self.acme_period_start > self.acme_period_end:
            raise UserError(_('Enter a valid execution period.'))
        lines = self.line_ids.filtered(lambda row: row.acme_site_quantity or row.quantity)
        if not lines:
            raise UserError(_('Enter site execution quantities before checking evidence.'))
        for line in lines:
            line._acme_check_ipc_evidence()
        self.with_context(acme_ipc_token=_IPC_TOKEN).write({
            'acme_verified_evidence': self._acme_evidence_signature(),
            'acme_evidence_verified_by_id': self.env.uid, 'acme_evidence_verified_at': fields.Datetime.now(),
        })
        self.message_post(body=_('The PM checked the evidence and site quantities for this IPC round.'))

    def _acme_pc_line_values(self, line):
        if self.acme_ipc_managed:
            line._acme_check_ipc_evidence()
            if self.acme_evidence_state != 'checked':
                raise UserError(_('The PM must verify the current evidence and site execution quantities.'))
            if line.quantity > line.acme_site_quantity:
                raise UserError(_('Current round quantity exceeds verified execution for %s.', line.display_name))
        return super()._acme_pc_line_values(line)

    def _acme_pc_fingerprint(self, invoice):
        data = super()._acme_pc_fingerprint(invoice)
        if self.acme_ipc_managed:
            data['evidence'] = self._acme_evidence_signature()
        return data

    def _acme_refresh_pc_previous_lines(self):
        legacy = self.filtered(lambda record: not record.acme_ipc_managed)
        super(IPCCertificate, legacy)._acme_refresh_pc_previous_lines()
        for line in (self - legacy).line_ids:
            quantity, value = line._acme_pc_history()
            if (quantity, value) != (line.previous_quantity, line.acme_pc_previous_value):
                line.with_context(acme_certify_token=_CERTIFY_TOKEN).write({
                    'previous_quantity': quantity, 'acme_pc_previous_value': value,
                })

    def _acme_pc_certify(self, invoice):
        super()._acme_pc_certify(invoice)
        if self.acme_ipc_managed:
            self.with_context(acme_ipc_token=_IPC_TOKEN, acme_certify_token=_CERTIFY_TOKEN).acme_ipc_output_snapshot = self._acme_ipc_build_outputs()

    def action_acme_pc_reopen(self):
        result = super().action_acme_pc_reopen()
        managed = self.filtered('acme_ipc_managed')
        managed.with_context(acme_ipc_token=_IPC_TOKEN).write({'acme_ipc_output_snapshot': False})
        managed.acme_pc_invoice_ids._acme_clear_invoice_signoffs()
        return result

    def _acme_ipc_build_outputs(self, legacy=False):
        self.ensure_one()
        invoice = self.invoice_id or self.acme_pc_invoice_ids
        project = self.project_id
        revisions = project.acme_boq_history_ids.filtered(lambda row: row.state == 'approved')
        historical_sections = {}
        if legacy:
            later = revisions.filtered(lambda row: self.certified_at and row.decision_at
                                       and row.decision_at > self.certified_at)
            for revision in later.sorted('number'):
                for change in revision.changes:
                    before = change['before']
                    if 'bill_no' in before or 'category' in before:
                        historical_sections.setdefault(change['line_id'], before.get('bill_no') or before.get('category'))
            revisions = revisions.filtered(lambda row: row.decision_at and self.certified_at
                                           and row.decision_at <= self.certified_at)
        sections = {}
        details = []
        currency = self.currency_id
        for line in self.line_ids:
            boq = line.boq_id
            section = historical_sections.get(boq.id, boq.bill_no or boq.category) or _('Unsectioned')
            contract = currency.round(line.contract_quantity * line.unit_rate)
            previous, current = line.acme_summary_previous, line.amount
            cumulative = previous + current
            details.append([
                section, line.reference or '', line.name, line.unit or '', line.unit_rate,
                line.contract_quantity, contract, line.previous_quantity, previous, line.quantity, current,
                line.cumulative_quantity, cumulative, line.acme_remaining_quantity, contract - cumulative,
            ])
            totals = sections.setdefault(section, [0, 0, 0])
            totals[0] += contract
            totals[1] += previous
            totals[2] += current
        summary = [[section, contract, prior, current, prior + current,
                    100 * (prior + current) / contract if contract else 0]
                   for section, (contract, prior, current) in sections.items()]
        cover = [
            [_('Round'), self.acme_ipc_round],
            [_('Project'), self.project_id.display_name],
            [_('Contract Reference'), self.project_id.acme_contract_reference or ''],
            [_('Contract Code'), self.project_id.acme_project_sequence or ''],
            [_('Client'), self.project_id.partner_id.display_name or ''],
            [_('Site'), self.acme_site or ''],
            [_('Period'), '%s – %s' % (self.acme_period_start or '', self.acme_period_end or '')],
            [_('Currency'), currency.name],
            [_('Current Gross Work'), self.work_amount],
            [_('Advance Recovery'), self.recovery_amount],
            [_('Retention'), self.retention_amount],
            [_('VAT'), invoice.amount_tax],
            [_('Withholding'), self.acme_pc_withholding],
            [_('Net Payable'), self.acme_pc_net_payable],
            [_('Prepared by'), self.acme_pc_pm_id.display_name or '', str(self.acme_pc_submitted_at or '')],
            [_('Evidence checked by'), self.acme_evidence_verified_by_id.display_name or '', str(self.acme_evidence_verified_at or '')],
            [_('Approved by'), self.acme_pc_dm_id.display_name or '', str(self.acme_pc_dm_at or '')],
            [_('Authorized by'), self.certified_by_id.display_name or '', str(self.certified_at or '')],
        ]
        variations = sum(
            change['after'].get('quantity', 0) * change['after'].get('selling_rate', 0)
            - change['before'].get('quantity', 0) * change['before'].get('selling_rate', 0)
            for revision in revisions
            for change in revision.changes
            if 'selling_rate' in change['after'] or 'selling_rate' in change['before']
        )
        return {'cover': cover, 'detail': details, 'summary': summary, 'client_excel': {
            'contractor': self.company_id.name, 'contract': project.acme_contract_amount,
            'variations': variations if not legacy or self.certified_at else None,
            'advance_percent': (project.acme_advance_percent
                if not legacy and project.acme_advance_type == 'percentage' else None),
            'rebate': self.acme_rebate_amount,
            'amount_words': currency.amount_to_text(self.acme_pc_net_payable),
        }}

    def _acme_ipc_outputs(self):
        self.ensure_one()
        self.check_access('read')
        if self.acme_certificate_type != 'progress' or self.state != 'certified':
            raise UserError(_('Authorize the IPC before printing or exporting client outputs.'))
        return self.acme_ipc_output_snapshot or self._acme_ipc_build_outputs(legacy=True)

    def action_acme_print_ipc(self):
        self._acme_ipc_outputs()
        return self.env.ref('acme_project_approvals.report_ipc_sheet').report_action(self)

    def action_acme_print_ipc_detail(self):
        self._acme_ipc_outputs()
        return self.env.ref('acme_project_approvals.report_ipc_detail').report_action(self)

    def action_acme_print_ipc_summary(self):
        self._acme_ipc_outputs()
        return self.env.ref('acme_project_approvals.report_ipc_summary').report_action(self)

    def action_acme_export_ipc(self):
        self._acme_ipc_outputs()
        return {'type': 'ir.actions.act_url', 'url': '/acme/ipc/%s/xlsx' % self.id, 'target': 'download'}

    def _acme_ipc_headers(self):
        return {
            'cover': [_('Description'), _('Amount / Details'), _('Date')],
            'detail': [_('Section'), _('Item'), _('Description'), _('Unit'), _('Unit Rate'),
                       _('Contract Qty'), _('Contract Value'), _('Previous Qty'), _('Previous Value'),
                       _('Current Qty'), _('Current Value'), _('Cumulative Qty'), _('Cumulative Value'),
                       _('Remaining Qty'), _('Remaining Value')],
            'summary': [_('BOQ Section'), _('Contract Value'), _('Previous Claimed'), _('Current Claimed'),
                        _('Cumulative Claimed'), _('Completion %')],
        }


class IPCMeasurement(models.Model):
    _inherit = 'acme.project.measurement'

    acme_site_quantity = fields.Float(string='Site Executed Qty', copy=False)
    acme_line_takeoff = fields.Binary(string='Item Take-off', attachment=True, copy=False)
    acme_grn = fields.Binary(string='GRN', attachment=True, copy=False)
    acme_storekeeper_certificate = fields.Binary(string='Storekeeper Certificate', attachment=True, copy=False)
    acme_delivery_note = fields.Binary(string='Delivery Note', attachment=True, copy=False)
    acme_client_acceptance = fields.Binary(string='Client Acceptance', attachment=True, copy=False)
    acme_accepted_quantity = fields.Float(string='Quantity on Client Acceptance', copy=False)
    acme_remaining_quantity = fields.Float(string='Remaining Quantity', compute='_compute_ipc_remaining')

    @api.depends('contract_quantity', 'cumulative_quantity')
    def _compute_ipc_remaining(self):
        for line in self:
            line.acme_remaining_quantity = line.contract_quantity - line.cumulative_quantity

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            certificate = self.env['acme.project.certificate'].browse(
                vals.get('certificate_id') or self.env.context.get('default_certificate_id') or []
            )
            if certificate.acme_certificate_type == 'progress':
                certificate.project_id._acme_require_site_editor()
                if vals.get('quantity'):
                    certificate._acme_pc_require_pm()
        return super().create(vals_list)

    def write(self, vals):
        progress = self.certificate_id.filtered(lambda certificate: certificate.acme_certificate_type == 'progress')
        if _LINE_EVIDENCE.intersection(vals) or 'quantity' in vals:
            progress.project_id._acme_require_site_editor()
        if 'quantity' in vals:
            for certificate in progress:
                certificate._acme_pc_require_pm()
        result = super().write(vals)
        if _LINE_EVIDENCE.intersection(vals) or 'quantity' in vals:
            progress._acme_ipc_invalidate_reviews()
        return result

    def unlink(self):
        progress = self.certificate_id.filtered(lambda certificate: certificate.acme_certificate_type == 'progress')
        progress.project_id._acme_require_site_editor()
        result = super().unlink()
        progress._acme_ipc_invalidate_reviews()
        return result

    def _acme_check_ipc_evidence(self):
        self.ensure_one()
        if not math.isfinite(self.acme_site_quantity) or self.acme_site_quantity <= 0:
            raise UserError(_('Enter a positive verified site quantity for %s.', self.display_name))
        if self.acme_billing_basis == 'measured':
            certificate = self.certificate_id
            if not (self.acme_line_takeoff or certificate.signed_takeoff):
                raise UserError(_('Attach take-off evidence for %s.', self.display_name))
            if self.search_count([
                ('boq_id', '=', self.boq_id.id), ('quantity', '>', 0), ('acme_billing_basis', '=', 'measured'),
                ('certificate_id', '!=', certificate.id), ('certificate_id.state', '=', 'certified'),
                ('certificate_id.acme_site', '=', certificate.acme_site),
                ('certificate_id.acme_period_start', '<=', certificate.acme_period_end),
                ('certificate_id.acme_period_end', '>=', certificate.acme_period_start),
            ], limit=1):
                raise UserError(_('This BOQ item already has measured work in an overlapping IPC period at this site.'))
        if self.acme_billing_basis == 'delivery':
            if not self.acme_client_acceptance or not self.acme_delivery_move_id or self.acme_accepted_quantity <= 0:
                raise UserError(_('Attach Client Acceptance for the linked delivery and enter the accepted quantity.'))
            if not self.acme_delivery_note:
                raise UserError(_('Attach the Delivery Note for this acceptance-billed item.'))
            delivery = self.acme_delivery_move_id
            site_stock = getattr(self.project_id, 'acme_staging_location_id', False)
            if (site_stock and delivery.location_id.parent_path.startswith(site_stock.parent_path)
                    and not (self.acme_grn and self.acme_storekeeper_certificate)):
                raise UserError(_('Attach the GRN and Storekeeper Certificate for delivery from Project Site stock.'))
            uom = self.boq_id.source_line_id.product_uom_id
            if (delivery.state != 'done' or delivery.location_dest_id.usage != 'customer'
                    or delivery.sale_line_id != self.boq_id.source_line_id or not uom):
                raise UserError(_('Select a completed customer delivery for this BOQ line.'))
            delivered = delivery.product_uom._compute_quantity(delivery.quantity, uom, round=False)
            prior, _value = self._acme_pc_history(delivery=delivery)
            if (not math.isfinite(self.acme_accepted_quantity)
                    or uom.compare(self.acme_accepted_quantity, delivered) > 0
                    or uom.compare(self.acme_site_quantity + prior, self.acme_accepted_quantity) > 0):
                raise UserError(_('The site quantity exceeds the remaining client-accepted delivery quantity.'))

    def _acme_pc_history(self, delivery=None, site=None):
        if self.certificate_id.acme_ipc_managed:
            site = None
        return super()._acme_pc_history(delivery=delivery, site=site)
