from odoo import api, fields, models, Command, _
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    acme_project_id = fields.Many2one('project.project', string='Created Project', readonly=True, copy=False)

    def action_acme_create_project_from_order(self):
        self.ensure_one()
        if self.state != 'sale':
            raise UserError(_('Project can only be created from a confirmed sales order.'))
        if self.acme_project_id:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'project.project',
                'res_id': self.acme_project_id.id,
                'view_mode': 'form',
            }

        existing = self.env['project.project'].search([('acme_order_id', '=', self.id)], limit=1)
        if existing:
            self.acme_project_id = existing.id
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'project.project',
                'res_id': existing.id,
                'view_mode': 'form',
            }

        project_code = self.env['ir.sequence'].next_by_code('acme.project.code') or '/'
        tender = getattr(self, 'x_tender_id', False)
        tender_code = getattr(tender, 'x_tender_code', False) if tender else False
        project_name = self.client_order_ref or self.name
        if tender:
            project_name = tender.name if not tender_code or tender_code in tender.name else f"[{tender_code}] {tender.name}"

        values = {
            'name': project_name,
            'company_id': self.company_id.id,
            'partner_id': self.partner_id.id,
            'acme_order_id': self.id,
            'acme_tender_id': tender.id if tender else False,
            'acme_contract_reference': tender_code or self.client_order_ref or self.name,
            'acme_contract_amount': self.amount_untaxed,
            'acme_contract_total': self.amount_total,
            'acme_contract_currency_id': self.currency_id.id,
            'acme_project_sequence': project_code,
            'acme_lifecycle_state': 'created',
            'acme_cost_budget': getattr(self, 'x_total_built_up_cost', 0.0),
            'acme_boq_vat_rate': getattr(tender, 'x_boq_vat_rate', 0.0) if tender else 0.0,
        }
        project = self.env['project.project'].create(values)
        self.acme_project_id = project.id

        # Import BOQ lines if present
        lines = self.order_line.filtered(lambda l: not l.display_type and not getattr(l, 'is_downpayment', False))
        if lines:
            try:
                from odoo.addons.acme_project_core.models.boq import _IMPORT_TOKEN
                token_ctx = {'acme_import_token': _IMPORT_TOKEN}
            except Exception:
                token_ctx = {}
            boq_vals = []
            for line in lines:
                boq_vals.append({
                    'project_id': project.id,
                    'source_line_id': line.id,
                    'sequence': line.sequence,
                    'reference': getattr(line, 'x_item_ref', False) or False,
                    'name': line.name,
                    'bill_no': getattr(line, 'x_bill_no', False) or False,
                    'category': line.x_bill_category_id.name if getattr(line, 'x_bill_category_id', False) else False,
                    'product_name': line.product_id.display_name,
                    'quantity': line.product_uom_qty,
                    'unit_rate': getattr(line, 'x_boq_unit_rate', 0.0) or line.price_unit,
                    'selling_rate': line.price_subtotal / line.product_uom_qty if line.product_uom_qty else 0,
                    'unit': line.product_uom_id.name if getattr(line, 'product_uom_id', False) else False,
                })
            self.env['acme.project.boq'].with_context(**token_ctx).create(boq_vals)
            project.with_context(**token_ctx).write({'acme_boq_initialized': True})
            lines.write({'project_id': project.id})

        project.message_post(body=_('Project created from confirmed Sales Order %s.') % self.name)
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'project.project',
            'res_id': project.id,
            'view_mode': 'form',
        }


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    def _timesheet_create_project(self):
        tender = getattr(self.order_id, 'x_tender_id', False)
        if tender or getattr(self.order_id, 'x_is_tender_quotation', False):
            project = self.env['project.project'].search([
                ('acme_order_id', '=', self.order_id.id)
            ], limit=1)
            if project:
                self.project_id = project
                return project
            return self.env['project.project']
        return super()._timesheet_create_project()
