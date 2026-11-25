from collections import defaultdict
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError
from .preparation import _STAFF_ADVANCE_KINDS


_REMINDER_INTERVAL = 15
_SETTLEMENT_CRON = object()
_REMINDER_FIELDS = {'acme_settlement_reminder_anchor', 'acme_settlement_reminder_due'}


class StaffSettlementRequest(models.Model):
    _inherit = 'approval.request'

    acme_staff_employee_id = fields.Many2one(
        'hr.employee', string='Payee / Employee', check_company=True, ondelete='restrict', copy=False,
    )
    acme_settlement_reminder_anchor = fields.Date(readonly=True, copy=False)
    acme_settlement_reminder_due = fields.Date(readonly=True, copy=False)

    def _acme_staff_payee(self):
        self.ensure_one()
        return self.acme_staff_employee_id or self.env['hr.employee'].search([
            ('company_id', '=', self.company_id.id), ('user_id', '=', self.request_owner_id.id),
        ])

    @api.constrains('acme_staff_employee_id', 'company_id')
    def _acme_check_staff_payee(self):
        for advance in self.filtered('acme_staff_employee_id'):
            if advance.acme_staff_employee_id.company_id != advance.company_id or advance.acme_staff_employee_id.user_id.share:
                raise ValidationError(_('Select an internal employee in the request company.'))

    @api.model_create_multi
    def create(self, vals_list):
        if any(_REMINDER_FIELDS.intersection(vals) for vals in vals_list) or any(
            'default_' + field in self.env.context for field in _REMINDER_FIELDS
        ):
            raise AccessError(_('Reminder cycles are maintained by the settlement scheduler.'))
        return super().create(vals_list)

    acme_settlement_move_ids = fields.One2many('account.move', 'acme_settlement_advance_id', string='Settlement Entries')
    acme_settlement_due_date = fields.Date(string='Settlement Due Date Override', tracking=True, copy=False)
    acme_settlement_company_currency_id = fields.Many2one(related='company_id.currency_id')
    acme_settlement_available = fields.Boolean(compute='_compute_staff_settlement')
    acme_settlement_due = fields.Date(compute='_compute_staff_settlement')
    acme_settlement_paid = fields.Monetary(compute='_compute_staff_settlement', currency_field='acme_settlement_company_currency_id')
    acme_settlement_applied = fields.Monetary(compute='_compute_staff_settlement', currency_field='acme_settlement_company_currency_id')
    acme_settlement_refunded = fields.Monetary(compute='_compute_staff_settlement', currency_field='acme_settlement_company_currency_id')
    acme_settlement_reimbursed = fields.Monetary(compute='_compute_staff_settlement', currency_field='acme_settlement_company_currency_id')
    acme_settlement_outstanding = fields.Monetary(compute='_compute_staff_settlement', currency_field='acme_settlement_company_currency_id')
    acme_settlement_excess_due = fields.Monetary(compute='_compute_staff_settlement', currency_field='acme_settlement_company_currency_id')

    def write(self, vals):
        if _REMINDER_FIELDS.intersection(vals) and self.env.context.get('acme_settlement_cron') is not _SETTLEMENT_CRON:
            raise AccessError(_('Reminder cycles are maintained by the settlement scheduler.'))
        if 'acme_staff_employee_id' in vals and self.filtered(lambda request: request.request_status != 'new'):
            raise UserError(_('Keep the original payee on submitted and historical staff advances.'))
        if 'acme_settlement_due_date' in vals and not self.env.user.has_group('account.group_account_user'):
            raise AccessError(_('Finance sets the settlement due date.'))
        return super().write(vals)

    def _acme_settlement_expense_total(self):
        expenses = self.env['hr.expense'].search([
            ('acme_staff_advance_id', '=', self.id), ('payment_mode', '=', 'own_account'),
            ('account_move_id.state', '=', 'posted'),
        ])
        total = 0
        for expense in expenses:
            move = expense.account_move_id
            original = sum(move.line_ids.filtered(lambda line: line.expense_id == expense).mapped('balance'))
            reversals = move._acme_posted_settlement_chain() - move
            corrected = original + sum(reversals.line_ids.filtered(lambda line: line.expense_id == expense).mapped('balance'))
            if original > 0:
                total += expense.total_amount * max(0, corrected / original)
        return total

    def _acme_staff_settlement_snapshot(self):
        self.ensure_one()
        company = self.company_id
        if (self.acme_kind not in _STAFF_ADVANCE_KINDS or self.request_status != 'approved'
                or not company.acme_staff_advance_account_id or not company.acme_staff_reimbursement_account_id):
            return False
        self.env['account.move'].check_access('read')
        self.env['hr.expense'].check_access('read')
        totals = defaultdict(float)
        dates = []
        for move in self.acme_settlement_move_ids.filtered(lambda entry: entry.state == 'posted'):
            amount = sum(entry._acme_settlement_control_balance(move.acme_settlement_account_id)
                         for entry in move._acme_posted_settlement_chain())
            role = move.acme_settlement_role
            totals[role] += amount if role in ('paid', 'reimbursed') else -amount
            if role == 'paid' and amount > 0:
                dates.append(move.date)
        currency = company.currency_id
        outstanding = currency.round(totals['paid'] - totals['applied'] - totals['refunded'])
        expenses = self._acme_settlement_expense_total()
        due = self.acme_settlement_due_date
        if not due and dates and company.acme_staff_settlement_days > 0:
            due = min(dates) + timedelta(days=company.acme_staff_settlement_days)
        return {**totals, 'due': due, 'disbursement_date': min(dates, default=False), 'outstanding': outstanding,
                'excess_due': currency.round(max(0, expenses - totals['applied'] - totals['reimbursed']))}

    @api.depends('acme_settlement_move_ids.state', 'acme_settlement_due_date', 'request_status')
    def _compute_staff_settlement(self):
        for request in self:
            try:
                data = request._acme_staff_settlement_snapshot()
            except AccessError:
                data = False
            request.acme_settlement_available = bool(data)
            request.acme_settlement_due = data.get('due') if data else False
            for key in ('paid', 'applied', 'refunded', 'reimbursed', 'outstanding', 'excess_due'):
                request[f'acme_settlement_{key}'] = data.get(key, 0) if data else 0

    def action_acme_open_settlement_entries(self):
        self.ensure_one()
        self.check_access('read')
        if not self.env.user.has_group('account.group_account_user'):
            raise AccessError(_('Only Finance may link staff advance accounting entries.'))
        if self.acme_kind not in _STAFF_ADVANCE_KINDS or self.request_status != 'approved':
            raise UserError(_('An approved mobilization advance is required.'))
        return {
            'type': 'ir.actions.act_window', 'name': _('Staff Advance Accounting Entries'),
            'res_model': 'account.move', 'view_mode': 'list,form',
            'domain': [('acme_settlement_advance_id', '=', self.id)],
            'context': {'default_acme_settlement_advance_id': self.id, 'default_company_id': self.company_id.id},
        }

    @api.model
    def _cron_acme_staff_settlements(self):
        today = fields.Date.context_today(self)
        activity_type = self.env.ref('acme_project_approvals.activity_staff_settlement')
        requests = self.search([('acme_kind', 'in', _STAFF_ADVANCE_KINDS), ('request_status', '=', 'approved'),
                                ('acme_settlement_move_ids', '!=', False)])
        for request in requests:
            self.env.cr.execute('SELECT id FROM approval_request WHERE id = %s FOR UPDATE SKIP LOCKED', [request.id])
            if not self.env.cr.fetchone():
                continue
            request.invalidate_recordset()
            data = request._acme_staff_settlement_snapshot()
            if not data:
                continue
            activities = request.activity_ids.filtered(lambda row: row.active and row.activity_type_id == activity_type)
            if data['outstanding'] <= 0:
                activities.action_feedback(feedback=_('The posted advance balance is settled.'))
                continue
            anchor = data['disbursement_date']
            cycles = (today - anchor).days // _REMINDER_INTERVAL if anchor else 0
            if cycles < 1:
                continue
            deadline = anchor + timedelta(days=cycles * _REMINDER_INTERVAL)
            employee = request._acme_staff_payee()
            user = (employee.user_id if len(employee) == 1 else False) or request.request_owner_id
            if not user.active:
                continue
            (activities - activities[:1]).action_feedback(feedback=_('Consolidated into the current settlement reminder.'))
            if request.acme_settlement_reminder_anchor == anchor and request.acme_settlement_reminder_due == deadline:
                continue
            if activities:
                activities[:1].write({'date_deadline': deadline, 'user_id': user.id})
            else:
                request.activity_schedule('acme_project_approvals.activity_staff_settlement',
                                          user_id=user.id, date_deadline=deadline)
            request.with_context(acme_settlement_cron=_SETTLEMENT_CRON).write({
                'acme_settlement_reminder_anchor': anchor, 'acme_settlement_reminder_due': deadline,
            })


class StaffSettlementMove(models.Model):
    _inherit = 'account.move'

    acme_settlement_advance_id = fields.Many2one('approval.request', check_company=True, ondelete='restrict', copy=False,
                                                string='Staff Advance Authorization')
    acme_settlement_expense_id = fields.Many2one('hr.expense', check_company=True, ondelete='restrict', copy=False,
                                                string='Expense Applied to Advance')
    acme_settlement_role = fields.Selection([
        ('paid', 'Advance Disbursement'), ('applied', 'Apply Posted Expense'),
        ('refunded', 'Cash Refund'), ('reimbursed', 'Extra Reimbursement'),
    ], string='Staff Settlement Entry', copy=False)
    acme_settlement_account_id = fields.Many2one('account.account', readonly=True, copy=False, ondelete='restrict')

    def _acme_posted_settlement_chain(self):
        self.ensure_one()
        moves = self.filtered(lambda move: move.state == 'posted')
        frontier = moves
        while frontier:
            frontier = frontier.reversal_move_ids.filtered(lambda move: move.state == 'posted') - moves
            moves |= frontier
        return moves

    def _acme_settlement_control_balance(self, account):
        return sum(self.line_ids.filtered(lambda line: line.account_id == account).mapped('balance'))

    _ACME_SETTLEMENT_LINKS = {'acme_settlement_advance_id', 'acme_settlement_expense_id', 'acme_settlement_role'}

    def _acme_validate_settlement_move(self):
        self.ensure_one()
        request = self.acme_settlement_advance_id
        role = self.acme_settlement_role
        expense = self.acme_settlement_expense_id
        company = self.company_id
        if (not role or request.acme_kind not in _STAFF_ADVANCE_KINDS or request.request_status != 'approved'
                or request.company_id != company or self.reversed_entry_id or self.move_type != 'entry'):
            raise ValidationError(_('Link a native journal entry to an approved staff advance from this company. Reversals inherit the original link.'))
        account = (company.acme_staff_reimbursement_account_id if role == 'reimbursed'
                   else company.acme_staff_advance_account_id)
        if not account or account.account_type != ('liability_payable' if role == 'reimbursed' else 'asset_current'):
            raise ValidationError(_('Configure the appropriate staff control account before linking the entry.'))
        partner = request._acme_staff_payee().work_contact_id
        control_lines = self.line_ids.filtered(lambda line: line.account_id == account)
        if len(partner) != 1 or not control_lines or control_lines.filtered(lambda line: line.partner_id != partner):
            raise ValidationError(_('The control-account lines must identify the employee work contact.'))
        amount = self._acme_settlement_control_balance(account)
        increasing = role in ('paid', 'reimbursed')
        if company.currency_id.compare_amounts(amount if increasing else -amount, 0) <= 0:
            raise ValidationError(_('The entry has the wrong debit or credit direction for this settlement.'))
        if role == 'applied':
            if (not expense or expense.acme_staff_advance_id != request or expense.account_move_id.state != 'posted'
                    or expense.payment_mode != 'own_account'):
                raise ValidationError(_('Apply only a posted employee-paid expense linked to this authorization.'))
            if not self.line_ids.filtered(lambda line: line.account_id == company.acme_staff_reimbursement_account_id
                                           and line.partner_id == partner and line.balance > 0):
                raise ValidationError(_('An application must debit the employee reimbursement payable account.'))
            self.env.cr.execute('UPDATE approval_request SET id = id WHERE id = %s', [request.id])
            other = self.search([('acme_settlement_expense_id', '=', expense.id), ('acme_settlement_role', '=', 'applied'),
                                 ('state', '=', 'posted'), ('id', '!=', self.id)])
            already = -sum(
                reversal._acme_settlement_control_balance(entry.acme_settlement_account_id)
                for entry in other for reversal in entry._acme_posted_settlement_chain()
            )
            if company.currency_id.compare_amounts(already - amount, expense.total_amount) > 0:
                raise ValidationError(_('The application exceeds the linked expense amount.'))
        elif expense or not (self.origin_payment_id or self.statement_line_id or
                              self.line_ids.filtered(lambda line: line.account_id.account_type == 'asset_cash')):
            raise ValidationError(_('Cash settlements must be traced to a native payment, bank statement or cash-account entry.'))
        return account

    @api.model_create_multi
    def create(self, vals_list):
        if any(self._ACME_SETTLEMENT_LINKS.intersection(vals) for vals in vals_list) or any(
            self.env.context.get(f'default_{key}') for key in self._ACME_SETTLEMENT_LINKS
        ):
            if not self.env.user.has_group('account.group_account_user'):
                raise AccessError(_('Only Finance may link staff settlement entries.'))
        return super().create(vals_list)

    def write(self, vals):
        if 'acme_settlement_account_id' in vals:
            raise AccessError(_('The settlement control account is recorded when the entry is linked or posted.'))
        if self._ACME_SETTLEMENT_LINKS.intersection(vals):
            if not self.env.user.has_group('account.group_account_user'):
                raise AccessError(_('Only Finance may link staff settlement entries.'))
            if self.filtered(lambda move: move.state == 'posted' and move.acme_settlement_advance_id):
                raise UserError(_('Posted settlement links are retained. Use a native reversal to correct the balance.'))
            result = super().write(vals)
            for move in self.filtered(lambda entry: entry.state == 'posted'):
                account = move._acme_validate_settlement_move()
                super(StaffSettlementMove, move).write({'acme_settlement_account_id': account.id})
            return result
        return super().write(vals)

    def _post(self, soft=True):
        for move in self.filtered(lambda entry: entry.acme_settlement_advance_id or entry.acme_settlement_role
                                  or entry.acme_settlement_expense_id):
            account = move._acme_validate_settlement_move()
            super(StaffSettlementMove, move).write({'acme_settlement_account_id': account.id})
        return super()._post(soft=soft)
