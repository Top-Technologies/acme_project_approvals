from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

POSITIONS = [('edm', 'EDM'), ('odm', 'ODM'), ('dm', 'DM'), ('dgm', 'DGM'), ('gm', 'GM')]
POSITION_RANK = {'edm': 1, 'odm': 2, 'dm': 3, 'dgm': 4, 'gm': 5}


class AcmeApprovalAuthority(models.Model):
    _name = 'acme.approval.authority'
    _description = 'ACME Approval Authority'
    _order = 'company_id, request_type, currency_id, amount_from, id'

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    request_type = fields.Selection([
        ('advance', 'Project Advance'),
        ('work_advance', 'Mobilization Work Advance'),
        ('petty_cash', 'Project Petty Cash'),
        ('variation', 'BOQ Variation'),
        ('purchase', 'Purchase Order'),
        ('ipc', 'IPC Customer Invoice'),
        ('guarantee', 'Guarantee Request'),
    ], required=True)
    company_id = fields.Many2one('res.company', required=True, ondelete='restrict', default=lambda self: self.env.company)
    currency_id = fields.Many2one('res.currency', required=True, ondelete='restrict', default=lambda self: self.env.company.currency_id)
    amount_from = fields.Monetary(required=True, default=0, currency_field='currency_id')
    amount_to = fields.Monetary(string='Amount To (exclusive)', currency_field='currency_id')
    effective_from = fields.Date(required=True, default=fields.Date.context_today)
    effective_to = fields.Date()
    approver_position = fields.Selection(POSITIONS, required=True)
    authorizer_position = fields.Selection(POSITIONS, required=True)

    @api.constrains('amount_from', 'amount_to', 'effective_from', 'effective_to', 'approver_position', 'authorizer_position')
    def _check_definition(self):
        for rule in self:
            if rule.amount_from < 0 or (rule.amount_to and rule.amount_to <= rule.amount_from):
                raise ValidationError(_('The upper amount must be greater than the lower amount.'))
            if rule.effective_to and rule.effective_to < rule.effective_from:
                raise ValidationError(_('The effective end date cannot precede the start date.'))
            if rule.approver_position == rule.authorizer_position:
                raise ValidationError(_('Reviewer and authorizer must be different positions.'))

    @api.model
    def matching_rule(self, company, currency, amount, request_type, on_date=None):
        on_date = on_date or fields.Date.context_today(self)
        rules = self.search([
            ('active', '=', True),
            ('request_type', '=', request_type),
            ('company_id', '=', company.id),
            ('currency_id', '=', currency.id),
            ('effective_from', '<=', on_date),
            '|', ('effective_to', '=', False), ('effective_to', '>=', on_date),
        ])
        if request_type in ('advance', 'ipc'):
            rules = rules.filtered(lambda rule: (
                currency.compare_amounts(amount, rule.amount_from) > 0
                if rule.authorizer_position == 'gm' and rule.amount_from else
                currency.compare_amounts(amount, rule.amount_from) >= 0
            ) and (not rule.amount_to or (
                currency.compare_amounts(amount, rule.amount_to) <= 0
                if rule.authorizer_position == 'dgm' else
                currency.compare_amounts(amount, rule.amount_to) < 0
            )))
        elif request_type in ('work_advance', 'petty_cash', 'variation'):
            rules = rules.filtered(lambda rule: (
                currency.compare_amounts(amount, rule.amount_from) > 0
                if ((request_type in ('work_advance', 'petty_cash') and rule.approver_position == 'dm')
                    or (request_type == 'variation' and rule.authorizer_position == 'dgm')) else
                currency.compare_amounts(amount, rule.amount_from) >= 0
            ) and (not rule.amount_to or currency.compare_amounts(amount, rule.amount_to) <= 0))
        else:
            rules = rules.filtered(lambda rule: rule.amount_from <= amount and (
                not rule.amount_to or amount < rule.amount_to
            ))
        if len(rules) != 1:
            raise UserError(_('No active authority rule matches this company, currency, amount and date.') if not rules else _('More than one authority rule matches this company, currency, amount and date.'))
        return rules

    def resolve_users(self, on_date=None):
        self.ensure_one()
        assignments = self.env['acme.approval.position.assignment']
        try:
            reviewer = assignments.for_position(self.company_id, self.approver_position, on_date).user_id
        except UserError:
            grp = self.env.ref(f'acme_project_approvals.group_acme_{self.approver_position}', raise_if_not_found=False) or self.env.ref(f'acme_project_core.group_acme_{self.approver_position}', raise_if_not_found=False)
            if grp and grp.users:
                reviewer = grp.users[0]
            else:
                raise
        try:
            authorizer = assignments.for_position(self.company_id, self.authorizer_position, on_date).user_id
        except UserError:
            grp = self.env.ref(f'acme_project_approvals.group_acme_{self.authorizer_position}', raise_if_not_found=False) or self.env.ref(f'acme_project_core.group_acme_{self.authorizer_position}', raise_if_not_found=False)
            if grp and grp.users:
                authorizer = grp.users[0]
            else:
                raise
        if reviewer == authorizer:
            raise UserError(_('The configured reviewer and authorizer resolve to the same user.'))
        return reviewer, authorizer


class AcmeApprovalPositionAssignment(models.Model):
    _name = 'acme.approval.position.assignment'
    _description = 'ACME Approval Position Assignment'
    _order = 'company_id, position, effective_from desc, id desc'

    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', required=True, ondelete='restrict')
    position = fields.Selection(POSITIONS, required=True)
    user_id = fields.Many2one('res.users', required=True, ondelete='restrict')
    effective_from = fields.Date(required=True, default=fields.Date.context_today)
    effective_to = fields.Date()

    @api.constrains('effective_from', 'effective_to')
    def _check_dates(self):
        if any(row.effective_to and row.effective_to < row.effective_from for row in self):
            raise ValidationError(_('The effective end date cannot precede the start date.'))

    @api.model
    def for_position(self, company, position, on_date=None):
        on_date = on_date or fields.Date.context_today(self)
        rows = self.search([
            ('active', '=', True),
            ('company_id', '=', company.id),
            ('position', '=', position),
            ('effective_from', '<=', on_date),
            '|', ('effective_to', '=', False), ('effective_to', '>=', on_date),
        ])
        if len(rows) != 1:
            pos_dict = dict(POSITIONS)
            pos_label = pos_dict.get(position, position)
            raise UserError(_('Configure exactly one active %(position)s assignment for %(company)s.', position=pos_label, company=company.display_name))
        return rows


class AcmeApprovalDelegation(models.Model):
    _name = 'acme.approval.delegation'
    _description = 'ACME Approval Delegation'
    _order = 'company_id, effective_from desc, id desc'

    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', required=True, ondelete='restrict')
    delegator_id = fields.Many2one('res.users', required=True, ondelete='restrict')
    delegator_position = fields.Selection(POSITIONS, required=True)
    delegate_id = fields.Many2one('res.users', required=True, ondelete='restrict')
    effective_from = fields.Date(required=True, default=fields.Date.context_today)
    effective_to = fields.Date()

    @api.constrains('delegator_id', 'delegate_id', 'delegator_position', 'effective_from', 'effective_to')
    def _check_delegation(self):
        for row in self:
            if row.delegator_id == row.delegate_id:
                raise ValidationError(_('A user cannot delegate to themselves.'))
            if row.delegator_position == 'edm':
                raise ValidationError(_('EDM has no lower position to delegate to.'))
            if row.effective_to and row.effective_to < row.effective_from:
                raise ValidationError(_('The effective end date cannot precede the start date.'))
            lower = [key for key, rank in POSITION_RANK.items() if rank == POSITION_RANK[row.delegator_position] - 1][0]
            if not self.env['acme.approval.position.assignment'].search_count([
                ('active', '=', True),
                ('company_id', '=', row.company_id.id),
                ('position', '=', lower),
                ('user_id', '=', row.delegate_id.id),
                ('effective_from', '<=', row.effective_from),
                '|', ('effective_to', '=', False), ('effective_to', '>=', row.effective_from),
            ]):
                raise ValidationError(_('The delegate must hold the immediately lower authority position.'))

    @api.model
    def is_valid_delegate(self, company, position, principal, user, on_date=None):
        if user == principal:
            return False
        on_date = on_date or fields.Date.context_today(self)
        return bool(self.search_count([
            ('active', '=', True),
            ('company_id', '=', company.id),
            ('delegator_id', '=', principal.id),
            ('delegate_id', '=', user.id),
            ('delegator_position', '=', position),
            ('effective_from', '<=', on_date),
            '|', ('effective_to', '=', False), ('effective_to', '>=', on_date),
        ]))
