from odoo import api, Command, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.addons.acme_project_core.models.certificate import _CERTIFY_TOKEN


_ADVANCE_CERTIFICATE = object()


class AdvanceCertificateRequest(models.Model):
    _inherit = 'approval.request'

    def action_acme_open_advance_certificate(self):
        self.ensure_one()
        self.check_access('read')
        if self.acme_kind != 'advance' or self.request_status != 'approved':
            raise UserError(_('An approved advance request is required.'))
        certificate = self._acme_get_advance_certificate()
        return {'type': 'ir.actions.act_window', 'res_model': 'acme.project.certificate',
                'view_mode': 'form', 'res_id': certificate.id}

    def _acme_get_advance_certificate(self):
        self.ensure_one()
        self.check_access('read')
        if self.acme_kind != 'advance' or self.request_status != 'approved':
            raise UserError(_('A finally authorized Advance Request is required.'))
        self.env.cr.execute('UPDATE approval_request SET id = id WHERE id = %s', [self.id])
        self.invalidate_recordset()
        if self.request_status != 'approved':
            raise UserError(_('The Advance Request is no longer authorized.'))
        certificates = self.env['acme.project.certificate']
        return certificates.search([('acme_advance_request_id', '=', self.id)], limit=1) or certificates.create({
            'project_id': self.acme_project_id.id, 'acme_certificate_type': 'advance',
            'acme_advance_request_id': self.id,
        })


class AdvanceCertificate(models.Model):
    _inherit = 'acme.project.certificate'

    acme_certificate_type = fields.Selection(
        [('progress', 'Progress'), ('advance', 'Advance')], default='progress', required=True,
    )
    acme_advance_request_id = fields.Many2one(
        'approval.request', check_company=True, ondelete='restrict', copy=False,
    )
    acme_requested_advance = fields.Monetary(related='acme_advance_request_id.acme_amount')
    acme_odm_approved_by = fields.Many2one('res.users', readonly=True, copy=False, tracking=True)
    acme_advance_paid = fields.Monetary(compute='_compute_advance_paid', store=True)
    _acme_request_unique = models.Constraint(
        'UNIQUE(acme_advance_request_id)', 'An advance request can have only one payment certificate.',
    )

    def _acme_requires_boq(self):
        return self.acme_certificate_type != 'advance'

    @api.constrains('acme_certificate_type', 'acme_advance_request_id', 'project_id', 'line_ids')
    def _check_advance_certificate(self):
        for certificate in self:
            request = certificate.acme_advance_request_id
            if certificate.acme_certificate_type == 'advance':
                if (request.acme_kind != 'advance' or request.request_status != 'approved'
                        or request.acme_project_id != certificate.project_id or certificate.line_ids):
                    raise ValidationError(_('An Advance certificate requires an approved advance request for this project and no measurements.'))
            elif request:
                raise ValidationError(_('Only Advance certificates can link an advance request.'))

    @api.model_create_multi
    def create(self, vals_list):
        if any('acme_odm_approved_by' in vals for vals in vals_list) or self.env.context.get('default_acme_odm_approved_by'):
            raise AccessError(_('Use ODM Approve to record certificate approval.'))
        return super().create(vals_list)

    def write(self, vals):
        if self.env.context.get('acme_advance_certificate') is not _ADVANCE_CERTIFICATE:
            if 'acme_odm_approved_by' in vals:
                raise AccessError(_('Use ODM Approve to record certificate approval.'))
            advances = self.filtered(lambda certificate: certificate.acme_certificate_type == 'advance')
            if ('invoice_id' in vals or 'line_ids' in vals) and advances:
                raise UserError(_('Use the Advance certificate workflow to create its invoice.'))
            if advances.filtered('acme_odm_approved_by') and {
                'project_id', 'acme_certificate_type', 'acme_advance_request_id', 'date',
            }.intersection(vals):
                raise UserError(_('Advance certificate details are locked after ODM approval.'))
        return super().write(vals)

    def action_certify(self):
        self.ensure_one()
        if self.acme_certificate_type == 'advance':
            raise UserError(_('Use ODM Approve and DM Authorize for Advance certificates.'))
        return super().action_certify()

    def _lock_advance_certificate(self):
        self.ensure_one()
        self.check_access('write')
        self.env.cr.execute('UPDATE acme_project_certificate SET id = id WHERE id = %s', [self.id])
        self.invalidate_recordset()
        if self.acme_certificate_type != 'advance':
            raise UserError(_('This action is for Advance certificates.'))
        self._check_advance_certificate()

    def action_acme_odm_approve_advance(self):
        if not self.env.user.has_group('acme_project_core.group_acme_odm'):
            raise AccessError(_('Only the ODM may approve an Advance certificate.'))
        self._lock_advance_certificate()
        if self.state != 'draft' or self.acme_odm_approved_by:
            raise UserError(_('Only a draft Advance certificate awaiting ODM approval can be approved.'))
        self.with_context(acme_advance_certificate=_ADVANCE_CERTIFICATE).acme_odm_approved_by = self.env.user

    def action_acme_dm_authorize_advance(self):
        return self._acme_load_advance_invoice()

    def _acme_load_advance_invoice(self, draft=None):
        self.ensure_one()
        self.check_access('write')
        request = self.acme_advance_request_id
        request.check_access('read')
        self.env.cr.execute('UPDATE approval_request SET id = id WHERE id = %s', [request.id])
        request.invalidate_recordset()
        self._lock_advance_certificate()
        if self.invoice_id and self.invoice_id.state != 'cancel':
            self.invoice_id.check_access('read')
            return self._acme_open_advance_invoice()
        if self.state not in ('draft', 'certified'):
            raise UserError(_('Use an eligible Advance certificate.'))
        project = self.project_id
        company = self.company_id
        product = company.acme_advance_invoice_product_id
        tax = company.acme_advance_invoice_tax_id
        if not product or not tax:
            raise UserError(_('Configure the Advance Invoice Product and Sales Tax on the company.'))
        Invoice = self.env['account.move'].with_company(company)
        invoice = Invoice.search([('acme_approval_id', '=', request.id), ('state', '!=', 'cancel')])
        if invoice:
            if len(invoice) != 1 or invoice.state != 'draft':
                raise UserError(_('An existing advance invoice requires Finance review before linking it to this certificate.'))
            invoice._check_acme_invoice()
            billed = invoice.invoice_line_ids.filtered(lambda line: line.display_type == 'product')
            if len(billed) != 1 or billed.product_id != product or billed.tax_ids != tax:
                raise UserError(_('The existing draft does not use the configured advance product and tax. Review it before linking.'))
        else:
            values = {
                'move_type': 'out_invoice',
                'company_id': company.id,
                'partner_id': project.partner_id.id,
                'currency_id': self.currency_id.id,
                'acme_project_id': project.id,
                'acme_approval_id': request.id,
                'acme_is_project': True, 'acme_invoice_payment_type': 'advance',
                'invoice_origin': self.display_name,
                'invoice_line_ids': [Command.create({
                    'product_id': product.id,
                    'name': request.acme_advance_description or request.name,
                    'quantity': 1,
                    'price_unit': request.acme_amount,
                    'tax_ids': [Command.set(tax.ids)],
                })],
            }
            if draft:
                draft.write({key: value for key, value in values.items() if key not in ('move_type', 'company_id')})
                invoice = draft
            else:
                invoice = Invoice.create(values)
        if self.currency_id.compare_amounts(invoice.amount_untaxed, request.acme_amount):
            raise UserError(_('The configured tax or product changed the untaxed invoice amount. Configure an untaxed unit price and retry.'))
        self.with_context(acme_advance_certificate=_ADVANCE_CERTIFICATE, acme_certify_token=_CERTIFY_TOKEN).write({
            'state': 'certified', 'certified_by_id': (self.certified_by_id or request.acme_advance_authorized_by or
                                                    request.approver_ids.filtered(
                                                        lambda row: row.status == 'approved'
                                                    ).sorted('sequence')[-1:].user_id).id,
            'certified_at': self.certified_at or request.acme_advance_authorized_at, 'invoice_id': invoice.id,
        })
        if self.currency_id.compare_amounts(invoice.amount_tax, request.acme_advance_vat):
            warning = _('Advance VAT mismatch: draft invoice VAT %(invoice)s, requisition VAT %(request)s.',
                        invoice=invoice.amount_tax, request=request.acme_advance_vat)
            self.message_post(body=warning)
            invoice.message_post(body=warning)
        return self._acme_open_advance_invoice()

    def _acme_open_advance_invoice(self):
        return {'type': 'ir.actions.act_window', 'res_model': 'account.move',
                'view_mode': 'form', 'res_id': self.invoice_id.id}

    @api.depends('acme_advance_request_id', 'invoice_id.state', 'invoice_id.amount_total', 'invoice_id.amount_untaxed',
                 'invoice_id.currency_id',
                 'invoice_id.line_ids.matched_credit_ids.credit_move_id.move_id.state',
                 'invoice_id.line_ids.matched_debit_ids.debit_move_id.move_id.state',
                 'invoice_id.line_ids.matched_credit_ids.credit_amount_currency',
                 'invoice_id.line_ids.matched_debit_ids.debit_amount_currency',
                 'invoice_id.line_ids.matched_credit_ids.credit_move_id.move_id.line_ids.balance',
                 'invoice_id.line_ids.matched_debit_ids.debit_move_id.move_id.line_ids.balance')
    def _compute_advance_paid(self):
        for certificate in self:
            invoice = certificate.invoice_id
            paid = 0
            if certificate.acme_advance_request_id and invoice.state == 'posted' and invoice.amount_total:
                for line in invoice.line_ids.filtered(lambda row: row.account_id.account_type == 'asset_receivable'):
                    for partial in line.matched_credit_ids:
                        paid += certificate._acme_cash_share(partial.credit_move_id) * abs(partial.debit_amount_currency)
                    for partial in line.matched_debit_ids:
                        paid += certificate._acme_cash_share(partial.debit_move_id) * abs(partial.credit_amount_currency)
                paid = invoice.currency_id.round(
                    invoice.amount_untaxed * min(paid, invoice.amount_total) / invoice.amount_total
                )
            certificate.acme_advance_paid = paid

    @api.model
    def _acme_cash_share(self, counterpart):
        move = counterpart.move_id
        payment = move.origin_payment_id
        if move.state != 'posted' or move.move_type != 'entry' or counterpart.balance >= 0:
            return 0
        if payment and payment.payment_type == 'inbound' and payment.partner_type == 'customer':
            liquidity, receivables, _write_offs = payment._seek_for_lines()
        elif move.statement_line_id:
            liquidity, _suspense, other = move.statement_line_id._seek_for_lines()
            receivables = other.filtered(lambda line: line.account_id.account_type == 'asset_receivable')
        else:
            return 0
        cash = sum(liquidity.mapped('balance'))
        receivable = -sum(receivables.mapped('balance'))
        return min(1, max(0, cash / receivable)) if receivable > 0 else 0


class ProjectAdvanceSettlement(models.Model):
    _inherit = 'project.project'

    acme_advance_paid = fields.Monetary(
        compute='_compute_advance_settlement', store=True, currency_field='acme_contract_currency_id',
    )
    acme_advance_received_flag = fields.Boolean(compute='_compute_advance_settlement', store=True)

    @api.depends('acme_certificate_ids.acme_advance_paid',
                 'acme_certificate_ids.acme_advance_request_id.request_status',
                 'acme_certificate_ids.acme_advance_request_id.acme_amount', 'acme_contract_currency_id')
    def _compute_advance_settlement(self):
        for project in self:
            advances = project.acme_certificate_ids.filtered(
                lambda certificate: certificate.acme_advance_request_id.request_status == 'approved'
            )
            paid = sum(advances.mapped('acme_advance_paid'))
            project.acme_advance_paid = paid
            project.acme_advance_received_flag = bool(advances and project.acme_contract_currency_id and
                project.acme_contract_currency_id.compare_amounts(
                    paid, sum(advances.acme_advance_request_id.mapped('acme_amount'))
                ) >= 0)

    @api.depends('acme_advance_paid')
    def _compute_recovered(self):
        super()._compute_recovered()
        for project in self:
            project.acme_advance_balance = project.acme_advance_paid - project.acme_recovered
