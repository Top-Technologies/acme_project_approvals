from odoo import fields, models


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    acme_approval_id = fields.Many2one('approval.request', string='Approved Request', readonly=True, copy=False)
