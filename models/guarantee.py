from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


class ProjectGuarantee(models.Model):
    _inherit = 'acme.project.guarantee'

    state = fields.Selection([
        ('draft', 'Draft'),
        ('approved', 'Approved'),
        ('active', 'Issued/Active'),
        ('expired', 'Expired'),
        ('released', 'Released'),
    ], string='Status', default='draft', required=True, tracking=True)

    signed_contract = fields.Binary(string='Signed Contract / Agreement', attachment=True, copy=False)
    signed_contract_filename = fields.Char(string='Signed Contract Filename')

    def action_approve(self):
        for guarantee in self:
            if guarantee.state not in ('draft', 'approved'):
                raise UserError(_('Only draft guarantees can be approved.'))
            guarantee.state = 'approved'
            guarantee.message_post(body=_('Guarantee approved and awaiting issuance / activation.'))

    def action_activate(self):
        for guarantee in self:
            if guarantee.state not in ('approved', 'draft'):
                raise UserError(_('Guarantee must be approved before activation.'))
            guarantee.state = 'active'
            guarantee.message_post(body=_('Guarantee activated / issued.'))

    def action_release(self):
        for guarantee in self:
            if guarantee.state != 'active':
                raise UserError(_('Only active guarantees can be released.'))
            guarantee.state = 'released'
            guarantee.message_post(body=_('Guarantee released.'))

    def _check_active_guarantees(self):
        for guarantee in self:
            if guarantee.state != 'active':
                raise UserError(_('Guarantee %(name)s is not active (current status: %(state)s).',
                                  name=guarantee.name, state=guarantee.state))

    @api.model
    def _cron_check_expiry(self):
        today = fields.Date.context_today(self)
        active_guarantees = self.search([
            ('state', '=', 'active'),
            ('expiry_date', '!=', False),
        ])
        for guarantee in active_guarantees:
            company = guarantee.company_id or self.env.company
            lead_days = company.acme_guarantee_lead_days or 30
            delta = (guarantee.expiry_date - today).days

            if delta < 0:
                guarantee.state = 'expired'
                guarantee.message_post(body=_('Guarantee expired on %s.') % guarantee.expiry_date)
            elif delta <= lead_days:
                activity_type = self.env.ref('mail.mail_activity_data_todo')
                existing = self.env['mail.activity'].search([
                    ('res_model', '=', self._name),
                    ('res_id', '=', guarantee.id),
                    ('activity_type_id', '=', activity_type.id),
                    ('summary', 'like', 'Guarantee Expiry%'),
                ], limit=1)
                if not existing:
                    recipient = guarantee.project_id.user_id or self.env.user
                    guarantee.activity_schedule(
                        activity_type_id=activity_type.id,
                        date_deadline=guarantee.expiry_date,
                        summary=_('Guarantee Expiry Reminder: %s') % guarantee.name,
                        note=_('Bank guarantee %s for project %s expires in %s days (on %s). Please take renewal or release action.') % (
                            guarantee.name, guarantee.project_id.name, delta, guarantee.expiry_date
                        ),
                        user_id=recipient.id,
                    )
