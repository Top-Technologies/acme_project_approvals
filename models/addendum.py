from odoo import api, Command, fields, models, _
from odoo.exceptions import AccessError, UserError


_ADDENDUM_TOKEN = object()


class VariationSalesAddendum(models.Model):
    _inherit = 'approval.request'

    def _acme_addendum_source_terms(self, source):
        order = source.order_id
        return {
            'order': order.id, 'company': order.company_id.id, 'currency': order.currency_id.id,
            'customer': order.partner_id.id, 'invoice_address': order.partner_invoice_id.id,
            'shipping_address': order.partner_shipping_id.id, 'pricelist': order.pricelist_id.id,
            'fiscal_position': order.fiscal_position_id.id, 'payment_term': order.payment_term_id.id,
            'product': source.product_id.id, 'uom': source.product_uom_id.id,
            'taxes': sorted(source.tax_ids.ids), 'price_unit': source.price_unit, 'discount': source.discount,
        }

    def _acme_addendum_expected(self):
        self.ensure_one()
        if self.acme_kind != 'variation' or self.request_status != 'approved' or not self.acme_variation_revision_id:
            raise UserError(_('An applied, authorized BOQ Variation is required.'))
        revision = self.acme_variation_revision_id
        if revision.state != 'approved' or revision.project_id != self.acme_project_id or len(revision.changes) != 1:
            raise UserError(_('This request has no matching approved BOQ change.'))
        change = revision.changes[0]
        before, after = change['before'], change['after']
        if not before:
            raise UserError(_('This BOQ line already came from the confirmed Sales Order. An addendum would double bill it.'))
        baseline = self.acme_variation_snapshot
        if 'lines' in baseline:
            baseline = next(row['baseline'] for row in baseline['lines'] if row['line_id'] == change['line_id'])
        currency = self.acme_currency_id
        quantity = after['quantity'] - before['quantity']
        previous_rate = before.get('selling_rate', baseline['selling_rate'])
        rate = after.get('selling_rate', previous_rate)
        if not currency or quantity <= 0 or rate != previous_rate:
            raise UserError(_('Only quantity increases at an unchanged selling rate can use this addendum. Reductions and price revisions require a separate commercial amendment.'))
        contract = self.acme_project_id.acme_order_id
        source = self.env['sale.order.line'].browse(baseline.get('source') or []).exists()
        if not source or not contract or contract.state != 'sale' or source.order_id != contract:
            raise UserError(_('The approved source line must belong to the confirmed original contract.'))
        terms = baseline.get('commercial')
        if not terms:
            if str(source.write_date or '') != baseline.get('source_revision'):
                raise UserError(_('This legacy variation has no frozen commercial terms, and its order line has changed. Review the contract before issuing an addendum.'))
            terms = self._acme_addendum_source_terms(source)
        if (terms['order'] != contract.id or terms['company'] != self.company_id.id
                or terms['currency'] != currency.id or terms['customer'] != contract.partner_id.id
                or not terms['product'] or not terms['uom']):
            raise UserError(_('The approved commercial terms no longer match the project contract.'))
        return terms, quantity, currency.round(after['quantity'] * rate - before['quantity'] * previous_rate)

    def action_acme_open_addendum(self):
        self.ensure_one()
        self.check_access('read')
        project = self.acme_project_id
        project._acme_check_preparation_editor()
        self.env.cr.execute('UPDATE approval_request SET id = id WHERE id = %s', [self.id])
        self.invalidate_recordset()
        if self.acme_kind != 'variation' or self.request_status != 'approved' or not self.acme_variation_revision_id:
            raise UserError(_('An applied, authorized BOQ Variation is required.'))
        Order = self.env['sale.order'].with_company(self.company_id)
        order = Order.search([('acme_addendum_variation_id', '=', self.id)], limit=1)
        if not order:
            terms, quantity, amount = self._acme_addendum_expected()
            change = self.acme_variation_revision_id.changes[0]
            contract = project.acme_order_id
            order = Order.with_context(acme_addendum_token=_ADDENDUM_TOKEN).create({
                'company_id': terms['company'], 'partner_id': terms['customer'],
                'partner_invoice_id': terms['invoice_address'], 'partner_shipping_id': terms['shipping_address'],
                'pricelist_id': terms['pricelist'], 'fiscal_position_id': terms['fiscal_position'],
                'payment_term_id': terms['payment_term'], 'warehouse_id': contract.warehouse_id.id,
                'project_id': project.id, 'acme_addendum_variation_id': self.id,
                'acme_original_contract_id': contract.id,
                'acme_addendum_terms': dict(terms, project=project.id, quantity=quantity, amount=amount),
                'origin': _('%(contract)s / Variation %(variation)s', contract=contract.name, variation=self.display_name),
                'order_line': [Command.create({
                    'product_id': terms['product'], 'product_uom_id': terms['uom'],
                    'product_uom_qty': quantity, 'price_unit': terms['price_unit'], 'discount': terms['discount'],
                    'tax_ids': [Command.set(terms['taxes'])], 'name': change['after']['name'],
                })],
            })
            order._acme_check_addendum_amount()
        return {'type': 'ir.actions.act_window', 'res_model': 'sale.order', 'view_mode': 'form', 'res_id': order.id}


class SaleOrderAddendum(models.Model):
    _inherit = 'sale.order'

    acme_addendum_variation_id = fields.Many2one(
        'approval.request', string='Authorized Variation', readonly=True, copy=False,
        check_company=True, ondelete='restrict',
    )
    acme_original_contract_id = fields.Many2one(
        'sale.order', string='Original Contract', readonly=True, copy=False,
        check_company=True, ondelete='restrict',
    )
    acme_addendum_terms = fields.Json(readonly=True, copy=False)
    _acme_addendum_unique = models.Constraint(
        'UNIQUE(acme_addendum_variation_id)', 'Each authorized variation may have only one Sales Order addendum.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        fields_to_protect = ('acme_addendum_variation_id', 'acme_original_contract_id', 'acme_addendum_terms')
        if self.env.context.get('acme_addendum_token') is not _ADDENDUM_TOKEN and any(
            vals.get(field, self.env.context.get(f'default_{field}'))
            for vals in vals_list for field in fields_to_protect
        ):
            raise AccessError(_('Create a commercial addendum from its authorized Variation.'))
        return super().create(vals_list)

    def write(self, vals):
        if {'acme_addendum_variation_id', 'acme_original_contract_id', 'acme_addendum_terms'}.intersection(vals):
            raise AccessError(_('Commercial addendum audit links cannot be edited.'))
        if 'project_id' in vals and self.filtered(
            lambda order: order.acme_addendum_variation_id and order.project_id.id != vals['project_id']
        ):
            raise AccessError(_('A commercial addendum must remain linked to its original project.'))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_variation_addendum(self):
        if self.filtered('acme_addendum_variation_id'):
            raise UserError(_('Keep the addendum for audit. Cancel it through Sales if required.'))

    def _acme_check_addendum_amount(self):
        for order in self.filtered('acme_addendum_variation_id'):
            terms = order.acme_addendum_terms
            if not terms:
                raise UserError(_('The frozen commercial terms for this addendum are missing.'))
            lines = order.order_line.filtered(lambda line: not line.display_type)
            if (order.project_id.id != terms['project'] or order.acme_original_contract_id.id != terms['order']
                    or order.company_id.id != terms['company'] or order.currency_id.id != terms['currency']
                    or order.partner_id.id != terms['customer'] or len(lines) != 1
                    or lines.product_id.id != terms['product'] or lines.product_uom_id.id != terms['uom']
                    or lines.product_uom_id.compare(lines.product_uom_qty, terms['quantity'])
                    or sorted(lines.tax_ids.ids) != terms['taxes']
                    or order.currency_id.compare_amounts(order.amount_untaxed, terms['amount'])):
                raise UserError(_('The addendum product, quantity, taxes or untaxed amount do not match the approved commercial delta. Review the source pricing and tax configuration.'))

    def _action_confirm(self):
        self._acme_check_addendum_amount()
        return super()._action_confirm()

    def _prepare_invoice(self):
        values = super()._prepare_invoice()
        if self.acme_addendum_variation_id:
            values['acme_project_id'] = self.project_id.id
        return values

    def _get_invoice_grouping_keys(self):
        keys = super()._get_invoice_grouping_keys()
        return keys if 'acme_project_id' in keys else [*keys, 'acme_project_id']


class SaleOrderLineAddendum(models.Model):
    _inherit = 'sale.order.line'

    def _timesheet_service_generation(self):
        lines = self.filtered(lambda line: not line.order_id.acme_addendum_variation_id)
        return super(SaleOrderLineAddendum, lines)._timesheet_service_generation()

    def _action_launch_stock_rule(self, *, previous_product_uom_qty=False):
        lines = self.filtered(lambda line: not line.order_id.acme_addendum_variation_id)
        return super(SaleOrderLineAddendum, lines)._action_launch_stock_rule(
            previous_product_uom_qty=previous_product_uom_qty,
        )
