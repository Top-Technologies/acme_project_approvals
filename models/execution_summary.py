import hashlib
import math
from collections import defaultdict

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import format_amount


_PREVIOUS_FIELDS = {
    'acme_summary_previous', 'acme_summary_prior_delta', 'acme_summary_prior_reason',
    'acme_summary_prior_evidence',
}
_EDIT_FIELDS = _PREVIOUS_FIELDS | {'acme_summary_current', 'acme_ipc_adjustment_reason'}


def _summary_table(env, rows, currency, caption):
    def amounts(values):
        return Markup('').join(Markup('<td class="text-end">%s</td>') % (
            format_amount(env, value, currency) if currency else format(value, ',.2f')
        ) for value in values)

    body = Markup('').join(Markup('<tr><td>%s</td>%s</tr>') % (row[0], amounts(row[1:5])) for row in rows)
    totals = [sum(row[col] for row in rows) for col in (1, 2, 3)]
    totals.append(totals[1] + totals[2])
    return Markup(
        '<p><strong>%s</strong></p><table class="table table-sm table-bordered">'
        '<thead class="table-light"><tr><th>%s</th><th class="text-end">%s</th>'
        '<th class="text-end">%s</th><th class="text-end">%s</th><th class="text-end">%s</th></tr></thead>'
        '<tbody>%s</tbody><tfoot class="table-light"><tr><th>%s</th>%s</tr></tfoot></table>'
    ) % (caption, _('BOQ Section'), _('Contract Amount'), _('Previous Executed'),
         _('Current Executed'), _('To Date'), body, _('Grand Total'), amounts(totals))


class ProjectExecutionSummary(models.Model):
    _inherit = 'project.project'

    @api.depends(
        'acme_boq_ids.quantity', 'acme_boq_ids.selling_rate', 'acme_boq_ids.bill_no', 'acme_boq_ids.category',
        'acme_contract_currency_id', 'acme_certificate_ids.state', 'acme_certificate_ids.number',
        'acme_certificate_ids.acme_ipc_output_snapshot',
    )
    @api.depends_context('uid', 'lang', 'company')
    def _compute_summary(self):
        super()._compute_summary()
        Certificate = self.env['acme.project.certificate']
        latest = {}
        if Certificate.has_access('read'):
            certificates = Certificate.search([
                ('project_id', 'in', self.ids), ('acme_certificate_type', '=', 'progress'),
                ('state', '=', 'certified'),
            ], order='number desc, id desc')
            for certificate in certificates:
                latest.setdefault(certificate.project_id.id, certificate)
        for project in self:
            totals = defaultdict(lambda: [0, 0, 0])
            currency = project.acme_contract_currency_id
            for line in project.acme_boq_ids:
                section = line.bill_no or line.category or _('Unsectioned')
                amount = line.quantity * line.selling_rate
                totals[section][0] += currency.round(amount) if currency else amount
            certificate = latest.get(project.id)
            if certificate and certificate.currency_id == currency:
                for section, _contract, previous, current, *_rest in certificate._acme_ipc_outputs()['summary']:
                    totals[section][1] += previous
                    totals[section][2] += current
                caption = _('Execution as of authorized %s · excluding VAT', certificate.acme_ipc_round)
            else:
                caption = _('No authorized IPC available · excluding VAT')
                if certificate:
                    caption = _('Authorized %s uses a different currency · execution unavailable', certificate.acme_ipc_round)
            rows = [[section, contract, previous, current, previous + current]
                    for section, (contract, previous, current) in sorted(totals.items())]
            project.acme_summary_html = _summary_table(self.env, rows, currency, caption)


class CertificateExecutionSummary(models.Model):
    _inherit = 'acme.project.certificate'

    acme_ipc_summary_html = fields.Html(compute='_compute_acme_ipc_summary_html')
    acme_ipc_summary_line_ids = fields.One2many('acme.project.measurement', 'certificate_id')

    @api.depends('state', 'number', 'currency_id', 'acme_ipc_output_snapshot', 'line_ids.amount',
                 'line_ids.acme_summary_previous', 'line_ids.contract_quantity', 'line_ids.unit_rate',
                 'line_ids.boq_id.bill_no', 'line_ids.boq_id.category')
    @api.depends_context('lang')
    def _compute_acme_ipc_summary_html(self):
        for cert in self:
            if cert.acme_certificate_type != 'progress' or cert.state == 'cancel':
                cert.acme_ipc_summary_html = False
                continue
            if cert.state == 'certified':
                rows = cert._acme_ipc_outputs()['summary']
            else:
                totals = defaultdict(lambda: [0, 0, 0])
                for line in cert.line_ids:
                    section = line.boq_id.bill_no or line.boq_id.category or _('Unsectioned')
                    totals[section][0] += line.acme_summary_contract
                    totals[section][1] += line.acme_summary_previous
                    totals[section][2] += line.amount
                rows = [[section, contract, previous, current, previous + current]
                        for section, (contract, previous, current) in totals.items()]
            cert.acme_ipc_summary_html = _summary_table(
                self.env, rows, cert.currency_id, _('%s · excluding VAT', cert.acme_ipc_round),
            )

    def _acme_validate_summary_edits(self):
        self.ensure_one()
        if not self.acme_source_bill_id:
            return
        for line in self.line_ids:
            quantity, value = line._acme_pc_history()
            if quantity != line.previous_quantity or self.currency_id.compare_amounts(value, line.acme_pc_previous_value):
                raise UserError(_('Prior billing changed. Refresh the draft progress invoice and resubmit the IPC.'))
            if line.acme_summary_prior_delta and (
                    not (line.acme_summary_prior_reason or '').strip() or not line.acme_summary_prior_evidence):
                raise UserError(_('Attach supporting evidence and enter a reason for the previous execution adjustment on %s.',
                                  line.display_name))

    def action_acme_pc_submit(self):
        self._acme_pc_lock()
        self._acme_pc_require_pm()
        self._acme_validate_summary_edits()
        return super().action_acme_pc_submit()

    def _acme_pc_check_review(self, role, user):
        invoice = super()._acme_pc_check_review(role, user)
        self._acme_validate_summary_edits()
        return invoice

    def _acme_pc_fingerprint(self, invoice):
        data = super()._acme_pc_fingerprint(invoice)
        corrected = self.with_context(bin_size=False).line_ids.filtered('acme_summary_prior_delta')
        if corrected:
            data['previous_execution_corrections'] = [
                [line.id, line.acme_summary_prior_delta, line.acme_summary_prior_reason,
                 hashlib.sha256(line.acme_summary_prior_evidence or b'').hexdigest()]
                for line in corrected.sorted('id')
            ]
        return data


class MeasurementExecutionSummary(models.Model):
    _inherit = 'acme.project.measurement'

    acme_summary_contract = fields.Monetary(string='Contract Amount', compute='_compute_summary_execution')
    acme_summary_previous = fields.Monetary(string='Previous Executed', compute='_compute_summary_execution',
                                            inverse='_inverse_summary_previous')
    acme_summary_current = fields.Monetary(string='Current Executed', compute='_compute_summary_execution',
                                           inverse='_inverse_summary_current')
    acme_summary_to_date = fields.Monetary(string='To Date', compute='_compute_summary_execution')
    acme_summary_prior_delta = fields.Monetary(copy=False, readonly=True)
    acme_summary_prior_reason = fields.Char(string='Previous Adjustment Reason', copy=False)
    acme_summary_prior_evidence = fields.Binary(string='Previous Adjustment Evidence', attachment=True, copy=False)

    @api.depends('currency_id', 'contract_quantity', 'unit_rate', 'amount', 'acme_pc_previous_value',
                 'acme_summary_prior_delta')
    def _compute_summary_execution(self):
        for line in self:
            contract = line.contract_quantity * line.unit_rate
            line.acme_summary_contract = line.currency_id.round(contract) if line.currency_id else contract
            line.acme_summary_previous = line.acme_pc_previous_value + line.acme_summary_prior_delta
            line.acme_summary_current = line.amount
            line.acme_summary_to_date = line.acme_summary_previous + line.amount

    def _inverse_summary_previous(self):
        for line in self:
            value = line.acme_summary_previous
            if not math.isfinite(value):
                raise UserError(_('Enter a finite previous execution amount.'))
            line.acme_summary_prior_delta = line.currency_id.round(value - line.acme_pc_previous_value)

    def _inverse_summary_current(self):
        for line in self:
            amount = line.acme_summary_current
            if not math.isfinite(amount) or amount < 0:
                raise UserError(_('Enter a finite nonnegative current execution amount.'))
            if line.currency_id.compare_amounts(amount, line.amount) == 0:
                continue
            if line.currency_id.compare_amounts(amount, line.acme_site_quantity * line.unit_rate) > 0:
                raise UserError(_('Return to Request Bill for approval of execution above the approved site amount.'))
            if not line.unit_rate:
                raise UserError(_('Enter an executed quantity in Detailed BOQ for a zero-rate item.'))
            quantity = amount / line.unit_rate
            uom = line.boq_id.source_line_id.product_uom_id
            quantity = min(uom.round(quantity) if uom else quantity, line.acme_site_quantity)
            if line.currency_id.compare_amounts(quantity * line.unit_rate, amount):
                raise UserError(_('This amount cannot be reconciled to the approved unit rate and unit of measure.'))
            line.quantity = quantity

    def write(self, vals):
        if _EDIT_FIELDS.intersection(vals):
            for line in self:
                cert = line.certificate_id
                if cert.state != 'draft' or not cert.acme_source_bill_id:
                    raise UserError(_('Execution adjustments are available on draft financial IPCs only.'))
                cert._acme_pc_require_pm()
        result = super().write(vals)
        if _EDIT_FIELDS.intersection(vals):
            self.certificate_id._acme_ipc_invalidate_reviews()
        return result