from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    acme_guarantee_lead_days = fields.Integer(
        string='Guarantee Expiry Lead Days',
        default=30,
        help='Number of days before guarantee expiry to trigger reminder activities.',
    )
    acme_guarantee_threshold = fields.Monetary(
        string='Guarantee Approval Threshold',
        default=500000.0,
        currency_field='currency_id',
        help='Guarantees at or below this threshold are authorized by DGM; above threshold by GM.',
    )


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    acme_guarantee_lead_days = fields.Integer(
        related='company_id.acme_guarantee_lead_days',
        readonly=False,
        string='Guarantee Expiry Lead Days',
    )
    acme_guarantee_threshold = fields.Monetary(
        related='company_id.acme_guarantee_threshold',
        readonly=False,
        string='Guarantee Approval Threshold',
    )
