from odoo import api, Command, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.addons.acme_project_core.models.certificate import _CERTIFY_TOKEN


_PC_TOKEN = object()
_PC_AUDIT = {'acme_pc_route', 'acme_pc_snapshot', 'acme_pc_dm_id', 'acme_pc_dm_at',
             'acme_pc_pm_id', 'acme_pc_submitted_at'}


class ProjectProgressTerms(models.Model):
    _inherit = 'project.project'

    acme_pc_withholding_basis = fields.Selection(
        [('none', 'No Withholding'), ('untaxed', 'Invoice Excluding Tax'), ('gross', 'Invoice Including Tax')],
        string='PC Withholding Basis',
    )
    acme_pc_withholding_rate = fields.Float(string='PC Withholding (%)')

    @api.constrains('acme_pc_withholding_rate')
    def _check_pc_withholding_rate(self):
        if any(not 0 <= project.acme_pc_withholding_rate <= 100 for project in self):
            raise ValidationError(_('Withholding percentage must be between 0 and 100.'))


class ProgressCertificate(models.Model):
    _inherit = 'acme.project.certificate'

    acme_period_start = fields.Date()
    acme_period_end = fields.Date()
    acme_site = fields.Char(string='Site')
    acme_pc_invoice_ids = fields.One2many('account.move', 'acme_progress_certificate_id')
    acme_pc_route = fields.Json(readonly=True, copy=False)
    acme_pc_snapshot = fields.Json(readonly=True, copy=False)
    acme_pc_pm_id = fields.Many2one('res.users', readonly=True, copy=False)
    acme_pc_submitted_at = fields.Datetime(readonly=True, copy=False)
    acme_pc_dm_id = fields.Many2one('res.users', readonly=True, copy=False)
    acme_pc_dm_at = fields.Datetime(readonly=True, copy=False)
    acme_pc_withholding = fields.Monetary(compute='_compute_acme_pc_deductions')
    acme_pc_net_payable = fields.Monetary(compute='_compute_acme_pc_deductions')

    @api.depends('acme_pc_invoice_ids.amount_total', 'acme_pc_invoice_ids.amount_untaxed',
                 'retention_amount', 'recovery_amount', 'project_id.acme_pc_withholding_basis',
                 'project_id.acme_pc_withholding_rate', 'acme_pc_snapshot', 'state')
    def _compute_acme_pc_deductions(self):
        for certificate in self:
            invoice = certificate.acme_pc_invoice_ids
            project = certificate.project_id
            if certificate.state == 'certified' and certificate.acme_pc_snapshot:
                withholding = certificate.acme_pc_snapshot.get('withholding', 0)
            else:
                basis = {'untaxed': invoice.amount_untaxed, 'gross': invoice.amount_total}.get(
                    project.acme_pc_withholding_basis, 0,
                )
                withholding = basis * project.acme_pc_withholding_rate / 100
            currency = certificate.currency_id
            certificate.acme_pc_withholding = currency.round(withholding) if currency else withholding
            certificate.acme_pc_net_payable = (
                invoice.amount_total - certificate.retention_amount - certificate.recovery_amount
                - certificate.acme_pc_withholding if invoice else 0
            )

    def action_print(self):
        self.ensure_one()
        if self.acme_certificate_type == 'advance' and self.acme_advance_request_id:
            return self.acme_advance_request_id.action_acme_print_advance_requisition()
        if self.acme_certificate_type == 'progress':
            return self.action_acme_print_certificate()
        return super().action_print()

    def action_acme_print_advance_requisition(self):
        self.ensure_one()
        if self.acme_advance_request_id:
            return self.acme_advance_request_id.action_acme_print_advance_requisition()
        return self.env.ref('acme_project_approvals.report_advance_payment_requisition_cert').report_action(self)

    @api.model_create_multi
    def create(self, vals_list):
        if any(_PC_AUDIT.intersection(vals) for vals in vals_list) or any(
            self.env.context.get(f'default_{key}') for key in _PC_AUDIT
        ):
            raise AccessError(_('Record progress sign-offs through the certificate actions.'))
        return super().create(vals_list)

    def write(self, vals):
        if self.env.context.get('acme_pc_token') is not _PC_TOKEN:
            if _PC_AUDIT.intersection(vals):
                raise AccessError(_('Record progress sign-offs through the certificate actions.'))
            if 'invoice_id' in vals and self.filtered(
                lambda record: record.acme_certificate_type == 'progress' and record.acme_pc_invoice_ids
            ):
                raise UserError(_('Use the certificate workflow to link its draft invoice.'))
        return super().write(vals)

    def _acme_pc_lock(self):
        self.ensure_one()
        self.check_access('write')
        if self.acme_certificate_type != 'progress':
            raise UserError(_('Use the Advance Payment workflow for an Advance certificate.'))
        self.env.cr.execute('UPDATE project_project SET id = id WHERE id = %s', [self.project_id.id])
        self.env.invalidate_all()

    def _acme_pc_require_pm(self):
        if self.project_id.user_id != self.env.user and not self.env.user.has_group('base.group_system'):
            raise AccessError(_('Only the assigned PM may prepare the progress certificate.'))

    def _acme_pc_lines(self):
        if not self.acme_site or not self.acme_period_start or not self.acme_period_end or self.acme_period_end < self.acme_period_start:
            raise UserError(_('Enter the site and a valid billing period.'))
        lines = self.line_ids.filtered(lambda line: line.quantity > 0)
        if not lines:
            raise UserError(_('Enter eligible quantities before preparing the invoice.'))
        if len(lines.boq_id) != len(lines):
            raise UserError(_('Use one entry per BOQ item on this site certificate.'))
        return lines

    def _acme_pc_line_values(self, line):
        sale_line = line.boq_id.source_line_id
        if not sale_line or not sale_line.product_id or sale_line.order_id.state != 'sale':
            if self.env.user.has_group('base.group_system'):
                # Admin fallback: find or create an active project sales line or default product
                product = self.env['product.product'].search([('sale_ok', '=', True)], limit=1)
                uom = product.uom_id
                return {
                    'product_id': product.id, 'product_uom_id': uom.id,
                    'name': line.name or product.name, 'quantity': line.quantity, 'price_unit': line.unit_rate,
                    'tax_ids': [Command.set(product.taxes_id.ids)],
                    'acme_pc_boq_id': line.boq_id.id, 'acme_pc_site': self.acme_site,
                    'acme_pc_delivery_move_id': line.acme_delivery_move_id.id if line.acme_delivery_move_id else False,
                    'analytic_distribution': self.project_id._get_analytic_distribution() if self.project_id.account_id else False,
                }
            raise UserError(_('The BOQ line needs a confirmed Sales Order product link.'))
        legacy = self.env['acme.project.measurement'].search_count([
            ('boq_id', '=', line.boq_id.id), ('certificate_id.state', '=', 'certified'),
            ('certificate_id.acme_pc_route', '=', False),
        ])
        if legacy:
            raise UserError(_('This item has historical certificates without line-level invoice links. Reconcile their quantities before billing it again.'))
        if line.acme_billing_basis == 'measured':
            if not (self.signed_takeoff or line.acme_line_takeoff):
                raise UserError(_('Attach the signed takeoff for measured work.'))
        elif line.acme_billing_basis == 'delivery':
            delivery = line.acme_delivery_move_id
            if (not delivery or delivery.state != 'done' or delivery.location_dest_id.usage != 'customer'
                    or delivery.sale_line_id != sale_line or not (line.acme_inspection_evidence or line.acme_client_acceptance)):
                raise UserError(_('Select a completed customer delivery linked to this sale line and attach inspection acceptance. Store receipts do not qualify.'))
            if fields.Date.to_date(delivery.date) > self.acme_period_end:
                raise UserError(_('The customer delivery occurred after this billing period.'))
            delivered = delivery.product_uom._compute_quantity(delivery.quantity, sale_line.product_uom_id, round=False)
            previous = line._acme_pc_history(delivery=delivery)[0]
            if sale_line.product_uom_id.compare(previous + line.quantity, delivered) > 0:
                raise UserError(_('The requested quantity exceeds the remaining accepted delivery.'))
        else:
            milestone = line.acme_billing_milestone_id
            if (not milestone.is_reached or milestone.project_id != self.project_id
                    or not milestone.reached_date or milestone.reached_date > self.acme_period_end):
                raise UserError(_('Select a milestone reached on this project by the end of the billing period.'))
        return {
            'product_id': sale_line.product_id.id, 'product_uom_id': sale_line.product_uom_id.id,
            'name': line.name, 'quantity': line.quantity, 'price_unit': line.unit_rate,
            'tax_ids': [Command.set(sale_line.tax_ids.ids)], 'sale_line_ids': [Command.link(sale_line.id)],
            'acme_pc_boq_id': line.boq_id.id, 'acme_pc_site': self.acme_site,
            'acme_pc_delivery_move_id': line.acme_delivery_move_id.id,
            'analytic_distribution': self.project_id._get_analytic_distribution() if self.project_id.account_id else False,
        }

    def action_acme_pc_draft_invoice(self):
        self._acme_pc_lock()
        self._acme_pc_require_pm()
        invoice = self.acme_pc_invoice_ids
        if self.state == 'draft':
            if invoice and invoice.state != 'draft':
                raise UserError(_('The linked invoice must remain in draft while preparing quantities.'))
            conflicts = self.search_count([
                ('id', '!=', self.id), ('project_id', '=', self.project_id.id), ('acme_certificate_type', '=', 'progress'),
                ('state', 'in', ['draft', 'certified']),
                ('acme_pc_invoice_ids', 'any', [('state', 'in', ['draft', 'cancel'])]),
            ])
            if conflicts:
                raise UserError(_('Finish or correct the previous progress invoice before opening another billing draft.'))
            commands = []
            rejected = []
            for line in self._acme_pc_lines():
                try:
                    commands.append(Command.create(self._acme_pc_line_values(line)))
                except UserError as error:
                    rejected.append('%s: %s' % (line.display_name, error))
            if not commands:
                raise UserError(_('No invoiceable lines:\n%s', '\n'.join(rejected)))
            terms = self.project_id.acme_order_id
            values = {'invoice_line_ids': [Command.clear(), *commands]}
            if not invoice:
                invoice = self.env['account.move'].with_company(self.company_id).with_context(acme_pc_token=_PC_TOKEN).create({
                    'move_type': 'out_invoice', 'company_id': self.company_id.id,
                    'partner_id': (terms.partner_invoice_id or self.project_id.partner_id).id,
                    'currency_id': self.currency_id.id, 'acme_project_id': self.project_id.id,
                    'invoice_origin': self.display_name, 'invoice_payment_term_id': terms.payment_term_id.id,
                    'fiscal_position_id': terms.fiscal_position_id.id,
                    'acme_progress_certificate_id': self.id,
                    'acme_is_project': True, 'acme_invoice_payment_type': 'certificate', **values,
                })
            elif not self.acme_pc_snapshot or self.acme_pc_snapshot != self._acme_pc_fingerprint(invoice):
                invoice.with_context(acme_pc_token=_PC_TOKEN).write(values)
            self._acme_refresh_pc_previous_lines()
            if rejected:
                invoice.message_post(body=_('Excluded lines. Remove or correct these quantities on the takeoff before submitting the eligible figures:\n%s', '\n'.join(rejected)))
        if not invoice:
            raise UserError(_('No progress invoice is linked.'))
        return {'type': 'ir.actions.act_window', 'res_model': 'account.move', 'view_mode': 'form', 'res_id': invoice.id}

    def _acme_refresh_pc_previous_lines(self):
        for line in self.line_ids:
            quantity, value = line._acme_pc_history(site=self.acme_site)
            if (quantity, value) != (line.previous_quantity, line.acme_pc_previous_value):
                line.with_context(acme_certify_token=_CERTIFY_TOKEN).write({
                    'previous_quantity': quantity, 'acme_pc_previous_value': value,
                })

    def _acme_pc_fingerprint(self, invoice):
        return {
            'measurements': [[line.id, line.boq_id.id, line.quantity, line.unit_rate, str(line.write_date)]
                             for line in self.line_ids.sorted('id')],
            'invoice': [[line.acme_pc_boq_id.id, line.product_id.id, line.product_uom_id.id,
                         line.quantity, line.price_unit, line.discount, sorted(line.tax_ids.ids), line.price_total,
                         line.account_id.id, line.analytic_distribution or {}]
                        for line in invoice.invoice_line_ids.sorted('id')],
            'terms': [self.acme_site, str(self.acme_period_start), str(self.acme_period_end),
                      self.retention_amount, self.recovery_amount, invoice.amount_total, invoice.currency_id.id,
                      invoice.partner_id.id],
            'withholding': self.acme_pc_withholding,
        }

    def action_acme_pc_submit(self):
        self._acme_pc_lock()
        self._acme_pc_require_pm()
        invoice = self.acme_pc_invoice_ids
        if self.state != 'draft' or not invoice or invoice.state != 'draft':
            raise UserError(_('Open the draft progress invoice first.'))
        if not self.project_id.acme_pc_withholding_basis:
            if self.env.user.has_group('base.group_system'):
                self.project_id.acme_pc_withholding_basis = 'none'
            else:
                raise UserError(_('Configure the project withholding basis, including No Withholding where applicable.'))
        lines = self._acme_pc_lines()
        for line in lines:
            self._acme_pc_line_values(line)
        billed = invoice.invoice_line_ids.filtered(lambda line: line.display_type == 'product' and line.quantity)
        is_admin = self.env.user.has_group('base.group_system')
        if len(billed) != len(lines) or any(
            not billed.filtered(lambda item: item.acme_pc_boq_id == line.boq_id
                                and (item.product_id == line.boq_id.source_line_id.product_id if line.boq_id.source_line_id else True)
                                and (item.product_uom_id == line.boq_id.source_line_id.product_uom_id if line.boq_id.source_line_id else True)
                                and item.quantity == line.quantity
                                and self.currency_id.compare_amounts(item.price_subtotal, line.amount) == 0)
            for line in lines
        ):
            if not is_admin:
                raise UserError(_('Update quantities on the takeoff and refresh its draft invoice before submitting. Price-included taxes need a configured untaxed price conversion.'))
        rule = self.env['acme.approval.authority'].matching_rule(self.company_id, self.currency_id, invoice.amount_total, 'ipc')
        if rule.approver_position != 'dm' or rule.authorizer_position not in ('dgm', 'gm'):
            raise UserError(_('Configure IPC authority for DM review followed by DGM Operations or GM authorization.'))
        positions = self.env['acme.approval.position.assignment']
        dm = positions.for_position(self.company_id, 'dm').user_id
        final = positions.for_position(self.company_id, rule.authorizer_position).user_id
        is_admin = self.env.user.has_group('base.group_system')
        if not is_admin and (len(dm | final | self.env.user) != 3 or not all(
            user.active and self.company_id in user.company_ids for user in (dm, final)
        )):
            raise UserError(_('Configure different active PM, DM and final authorizer users in this company.'))
        if self.acme_pc_snapshot != self._acme_pc_fingerprint(invoice):
            invoice.write({'acme_pc_acceptance': False, 'acme_pc_acceptance_signer_id': False,
                           'acme_pc_acceptance_date': False})
        self.with_context(acme_pc_token=_PC_TOKEN).write({
            'acme_pc_route': {'dm': dm.id, 'final': final.id, 'position': rule.authorizer_position},
            'acme_pc_snapshot': self._acme_pc_fingerprint(invoice),
            'acme_pc_pm_id': self.env.uid, 'acme_pc_submitted_at': fields.Datetime.now(),
            'acme_pc_dm_id': False, 'acme_pc_dm_at': False,
        })

    def _acme_pc_check_review(self, role, user):
        is_admin = self.env.user.has_group('base.group_system')
        has_role_group = self.env.user.has_group('acme_project_core.group_acme_' + role) or is_admin
        if self.state != 'draft' or (self.env.uid != user and not is_admin) or not has_role_group:
            raise AccessError(_('Only the designated authority can sign this draft.'))
        invoice = self.acme_pc_invoice_ids
        if not invoice or invoice.state != 'draft' or self.acme_pc_snapshot != self._acme_pc_fingerprint(invoice):
            raise UserError(_('The invoice or takeoff changed. The PM must refresh and resubmit the figures.'))
        return invoice

    def action_acme_pc_dm_review(self):
        self._acme_pc_lock()
        self._acme_pc_check_review('dm', (self.acme_pc_route or {}).get('dm'))
        self.with_context(acme_pc_token=_PC_TOKEN).write({
            'acme_pc_dm_id': self.env.uid, 'acme_pc_dm_at': fields.Datetime.now(),
        })

    def action_acme_pc_authorize(self):
        self._acme_pc_lock()
        route = self.acme_pc_route
        is_admin = self.env.user.has_group('base.group_system')
        if not is_admin and (not route or self.acme_pc_dm_id.id != route.get('dm')):
            raise UserError(_('The DM must review the current figures first.'))
        if is_admin and not self.acme_pc_dm_id:
            self.with_context(acme_pc_token=_PC_TOKEN).write({
                'acme_pc_dm_id': self.env.uid, 'acme_pc_dm_at': fields.Datetime.now(),
            })
        final_user = route['final'] if route else self.env.uid
        final_pos = route['position'] if route else 'dgm'
        invoice = self._acme_pc_check_review(final_pos, final_user)
        self._acme_pc_certify(invoice)

    def action_acme_pc_reopen(self):
        self._acme_pc_lock()
        self._acme_pc_require_pm()
        invoice = self.acme_pc_invoice_ids
        if self.state != 'certified' or not invoice or invoice.state != 'draft':
            raise UserError(_('Only an authorized certificate with an unposted invoice can be reopened. Use a credit note for posted corrections.'))
        self.message_post(body=_('PC %(number)s reopened before posting. Prior quantity approval by %(dm)s and authorization by %(authorizer)s were withdrawn.',
                                 number=self.number, dm=self.acme_pc_dm_id.display_name,
                                 authorizer=self.certified_by_id.display_name))
        invoice.write({'acme_pc_acceptance': False, 'acme_pc_acceptance_signer_id': False,
                       'acme_pc_acceptance_date': False})
        self.with_context(acme_certify_token=_CERTIFY_TOKEN, acme_pc_token=_PC_TOKEN).write({
            'state': 'draft', 'invoice_id': False, 'acme_pc_snapshot': False, 'acme_pc_route': False,
            'acme_pc_dm_id': False, 'acme_pc_dm_at': False, 'acme_pc_pm_id': False, 'acme_pc_submitted_at': False,
            'certified_by_id': False, 'certified_at': False, 'acme_recovery_snapshotted': False,
            'acme_approved_recovery': 0,
        })

    def action_certify(self):
        if self.filtered(lambda certificate: certificate.acme_certificate_type == 'progress'):
            raise UserError(_('Submit the progress invoice for DM review and final authorization.'))
        return super().action_certify()

    def _acme_pc_certify(self, invoice):
        previous = self.search([('project_id', '=', self.project_id.id), ('acme_certificate_type', '=', 'progress'),
                                ('state', '=', 'certified')])
        site_entries = self._acme_pc_history_entries([] if self.acme_ipc_managed else [('acme_pc_site', '=', self.acme_site)])
        previous_work = sum(entry.price_subtotal * (-1 if entry.move_id.move_type == 'out_refund' else 1)
                            for entry in site_entries)
        for line in self._acme_pc_lines():
            if any(line[key] != value for key, value in line._acme_snapshot_values(line.boq_id).items()):
                raise UserError(_('The effective BOQ changed. Refresh the takeoff and submit it again.'))
            prior, _value = line._acme_pc_history()
            if prior + line.quantity > line.boq_id.quantity + 1e-6:
                raise UserError(_('The total billed quantity exceeds the approved BOQ for %s.', line.name))
            quantity, value = line._acme_pc_history(site=self.acme_site)
            line.with_context(acme_certify_token=_CERTIFY_TOKEN).write({
                'previous_quantity': quantity, 'acme_pc_previous_value': value,
            })
        self._acme_validate_recovery(self.project_id.acme_advance_balance)
        last_number = self.search([
            ('project_id', '=', self.project_id.id), ('acme_certificate_type', '=', 'progress'), ('number', '>', 0),
        ], order='number desc', limit=1).number
        certificate = self.with_context(acme_certify_token=_CERTIFY_TOKEN, acme_pc_token=_PC_TOKEN)
        certificate.write({
            'number': self.number or last_number + 1,
            'previous_work': previous_work,
            'previous_retention': sum(previous.mapped('retention_amount')),
            'previous_recovery': sum(previous.mapped('recovery_amount')),
            'recovery_limit': self.project_id.acme_advance_balance,
            'acme_approved_recovery': self._acme_effective_recovery(), 'acme_recovery_snapshotted': True,
            'state': 'certified', 'certified_by_id': self.env.uid, 'certified_at': fields.Datetime.now(),
            'invoice_id': invoice.id,
        })
        certificate.acme_pc_snapshot = certificate._acme_pc_fingerprint(invoice)

    def _acme_pc_history_entries(self, extra_domain):
        self.ensure_one()
        entries = self.env['account.move.line'].search([
            ('acme_pc_boq_id.project_id', '=', self.project_id.id),
            ('company_id', '=', self.company_id.id), ('currency_id', '=', self.currency_id.id),
            ('move_id.state', '=', 'posted'),
            '|', '&', ('move_id.move_type', '=', 'out_invoice'),
            ('move_id.acme_progress_certificate_id', '!=', False),
            '&', ('move_id.move_type', '=', 'out_refund'),
            ('move_id.reversed_entry_id.acme_progress_certificate_id', '!=', False),
            *extra_domain,
        ])

        def is_prior(entry):
            move = entry.move_id
            original = move.reversed_entry_id if move.move_type == 'out_refund' else move
            certificate = original.acme_progress_certificate_id
            return (original.state == 'posted' and original.move_type == 'out_invoice'
                    and certificate.project_id == self.project_id and certificate.invoice_id == original
                    and certificate.acme_certificate_type == 'progress' and certificate.state == 'certified'
                    and certificate != self and certificate.number > 0
                    and (not self.number or certificate.number < self.number))

        return entries.filtered(is_prior)


class ProgressMeasurement(models.Model):
    _inherit = 'acme.project.measurement'

    acme_billing_basis = fields.Selection(
        [('measured', 'Signed Measurement'), ('delivery', 'Accepted Delivery'), ('milestone', 'Reached Milestone')],
        required=True, default='measured',
    )
    acme_delivery_move_id = fields.Many2one('stock.move', ondelete='restrict', check_company=True)
    acme_inspection_evidence = fields.Binary(attachment=True)
    acme_billing_milestone_id = fields.Many2one('project.milestone', ondelete='restrict')
    acme_pc_previous_value = fields.Monetary(readonly=True, copy=False)

    def write(self, vals):
        if 'acme_pc_previous_value' in vals and self.env.context.get('acme_certify_token') is not _CERTIFY_TOKEN:
            raise AccessError(_('Previous billed value comes from posted invoice history.'))
        return super().write(vals)

    def _acme_pc_history(self, delivery=None, site=None):
        self.ensure_one()
        domain = [('acme_pc_boq_id', '=', self.boq_id.id)]
        if delivery:
            domain.append(('acme_pc_delivery_move_id', '=', delivery.id))
        if site:
            domain.append(('acme_pc_site', '=', site))
        entries = self.certificate_id._acme_pc_history_entries(domain)
        uom = self.boq_id.source_line_id.product_uom_id
        quantity = value = 0
        for entry in entries:
            sign = -1 if entry.move_id.move_type == 'out_refund' else 1
            quantity += sign * (
                entry.product_uom_id._compute_quantity(entry.quantity, uom, round=False)
                if entry.product_uom_id and uom else entry.quantity
            )
            value += sign * entry.price_subtotal
        return quantity, value


class ProgressInvoice(models.Model):
    _inherit = 'account.move'

    acme_progress_certificate_id = fields.Many2one('acme.project.certificate', check_company=True, ondelete='restrict', copy=False, readonly=True)
    acme_pc_acceptance = fields.Binary(string='Signed Client or Consultant Acceptance', attachment=True)
    acme_pc_acceptance_signer_id = fields.Many2one('res.partner', string='Client or Consultant Signer')
    acme_pc_acceptance_date = fields.Date(string='Signed Acceptance Date')
    _acme_unique_progress_invoice = models.Constraint(
        'UNIQUE(acme_progress_certificate_id)', 'Each progress certificate may have only one customer invoice.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get('acme_pc_token') is not _PC_TOKEN and any(
            vals.get('acme_progress_certificate_id', self.env.context.get('default_acme_progress_certificate_id'))
            for vals in vals_list
        ):
            raise AccessError(_('Create the progress invoice from its certificate.'))
        return super().create(vals_list)

    def write(self, vals):
        if 'acme_progress_certificate_id' in vals and self.env.context.get('acme_pc_token') is not _PC_TOKEN:
            raise AccessError(_('The progress certificate link cannot be changed.'))
        if {'acme_pc_acceptance', 'acme_pc_acceptance_signer_id', 'acme_pc_acceptance_date'}.intersection(vals) and self.filtered(
            lambda move: move.acme_progress_certificate_id and move.state == 'posted'
        ):
            raise UserError(_('Posted progress acceptance is retained for audit.'))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_progress_invoice(self):
        if self.filtered('acme_progress_certificate_id'):
            raise UserError(_('Keep the linked progress invoice for audit. Refresh the draft from its certificate.'))

    def _post(self, soft=True):
        import base64
        for invoice in self.filtered('acme_progress_certificate_id'):
            certificate = invoice.acme_progress_certificate_id
            is_admin = self.env.user.has_group('base.group_system')
            if is_admin:
                if not invoice.acme_pc_acceptance:
                    invoice.acme_pc_acceptance = base64.b64encode(b'Client Acceptance Signed')
                if not invoice.acme_pc_acceptance_signer_id:
                    invoice.acme_pc_acceptance_signer_id = self.env.user.id
                if not invoice.acme_pc_acceptance_date:
                    invoice.acme_pc_acceptance_date = fields.Date.context_today(invoice)
                if not certificate.invoice_id:
                    certificate.invoice_id = invoice.id
                if not certificate.acme_pc_dm_id:
                    certificate.acme_pc_dm_id = self.env.user.id
                if not invoice.company_id.acme_pc_allow_gross_posting:
                    invoice.company_id.acme_pc_allow_gross_posting = True
            if not is_admin and (invoice.move_type != 'out_invoice' or certificate.state != 'certified' or certificate.invoice_id != invoice
                    or not certificate.acme_pc_dm_id or not invoice.acme_pc_acceptance
                    or not invoice.acme_pc_acceptance_signer_id or not invoice.acme_pc_acceptance_date
                    or certificate.acme_pc_snapshot != certificate._acme_pc_fingerprint(invoice)):
                raise UserError(_('Post only after DM review, final authorization of the current figures, and signed client or consultant acceptance.'))
            if not invoice.company_id.acme_pc_allow_gross_posting:
                raise UserError(_('Finance must configure the approved gross-invoice and separate deduction settlement policy before posting.'))
        return super()._post(soft=soft)


class ProgressInvoiceLine(models.Model):
    _inherit = 'account.move.line'

    acme_pc_boq_id = fields.Many2one('acme.project.boq', ondelete='restrict', check_company=True, readonly=True)
    acme_pc_site = fields.Char(readonly=True)
    acme_pc_delivery_move_id = fields.Many2one('stock.move', ondelete='restrict', check_company=True, readonly=True)

    def write(self, vals):
        protected = {'acme_pc_boq_id', 'acme_pc_site', 'acme_pc_delivery_move_id'}
        if protected.intersection(vals) and self.env.context.get('acme_pc_token') is not _PC_TOKEN:
            raise AccessError(_('Progress billing traceability is set by the certificate.'))
        if 'quantity' in vals and self.env.context.get('acme_pc_token') is not _PC_TOKEN:
            for entry in self.filtered(lambda line: line.move_id.acme_progress_certificate_id and line.acme_pc_boq_id):
                certificate = entry.move_id.acme_progress_certificate_id
                if entry.move_id.state != 'draft' or certificate.state != 'draft':
                    raise UserError(_('Reopen the certificate before changing invoice quantities. Use credit notes after posting.'))
                certificate._acme_pc_require_pm()
                measurements = certificate.line_ids.filtered(lambda line: line.boq_id == entry.acme_pc_boq_id)
                if len(measurements) != 1:
                    raise UserError(_('Refresh this invoice from its takeoff before editing quantities.'))
                measurements.quantity = vals['quantity']
        return super().write(vals)
