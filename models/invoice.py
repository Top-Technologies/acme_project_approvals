from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError, UserError

_INVOICE_SIGNOFF = object()
_INVOICE_POST = object()
_SIGNOFF_FIELDS = {
    'acme_invoice_odm_id', 'acme_invoice_odm_at', 'acme_invoice_dm_id', 'acme_invoice_dm_at',
    'acme_invoice_snapshot',
}
_REVIEW_FIELDS = {
    'acme_is_project', 'acme_invoice_payment_type', 'acme_invoice_advance_source_id',
    'acme_invoice_pc_source_id', 'acme_project_id', 'acme_approval_id', 'acme_progress_certificate_id',
    'invoice_line_ids', 'line_ids', 'partner_id', 'currency_id', 'company_id', 'fiscal_position_id',
    'invoice_payment_term_id', 'invoice_date', 'invoice_date_due', 'journal_id',
}


class AccountMove(models.Model):
    _inherit = 'account.move'
    acme_approval_id = fields.Many2one('approval.request', string='Advance Approval', ondelete='restrict', check_company=True, copy=False)

    acme_is_project = fields.Boolean(string='Is Project', copy=False)
    acme_invoice_payment_type = fields.Selection(
        [('advance', 'Advance'), ('certificate', 'Payment Certificate')], string='Payment Type', copy=False,
    )
    acme_invoice_advance_source_id = fields.Many2one(
        'approval.request', string='Advance Request', check_company=True, copy=False,
    )
    acme_invoice_pc_source_id = fields.Many2one(
        'acme.project.certificate', string='Payment Certificate', check_company=True, copy=False,
    )
    acme_invoice_odm_id = fields.Many2one('res.users', string='Invoice Approved by ODM', readonly=True, copy=False)
    acme_invoice_odm_at = fields.Datetime(string='Invoice Approved on', readonly=True, copy=False)
    acme_invoice_dm_id = fields.Many2one('res.users', string='Invoice Authorized by DM', readonly=True, copy=False)
    acme_invoice_dm_at = fields.Datetime(string='Invoice Authorized on', readonly=True, copy=False)
    acme_invoice_snapshot = fields.Json(readonly=True, copy=False)
    acme_invoice_retention = fields.Monetary(related='acme_progress_certificate_id.retention_amount')
    acme_invoice_recovery = fields.Monetary(related='acme_progress_certificate_id.recovery_amount')
    acme_invoice_withholding = fields.Monetary(related='acme_progress_certificate_id.acme_pc_withholding')
    acme_invoice_net_payable = fields.Monetary(related='acme_progress_certificate_id.acme_pc_net_payable')

    def init(self):
        super().init()
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS acme_invoice_active_advance_uniq
            ON account_move (acme_approval_id) WHERE acme_approval_id IS NOT NULL AND state != 'cancel'
        """)

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get('default_state') == 'posted':
            raise UserError(_('Create the invoice in draft before posting.'))
        if any(_SIGNOFF_FIELDS.intersection(vals) for vals in vals_list) or any(
            'default_' + name in self.env.context for name in _SIGNOFF_FIELDS
        ):
            raise AccessError(_('Record invoice decisions using the approval actions.'))
        return super().create(vals_list)

    @api.onchange('acme_project_id')
    def _onchange_acme_invoice_project(self):
        for move in self.filtered(lambda rec: rec.move_type == 'out_invoice' and rec.acme_project_id
                                 and not (rec.acme_approval_id or rec.acme_progress_certificate_id)):
            move.partner_id = move.acme_project_id.partner_id
            move.currency_id = move.acme_project_id.acme_contract_currency_id
            move.acme_invoice_advance_source_id = False
            move.acme_invoice_pc_source_id = False

    def action_acme_load_invoice_source(self):
        self.ensure_one()
        self.check_access('write')
        self.env.cr.execute('UPDATE account_move SET id = id WHERE id = %s', [self.id])
        self.invalidate_recordset()
        if self.state != 'draft' or self.move_type != 'out_invoice' or not self.acme_project_id:
            raise UserError(_('Select a project on a draft customer invoice.'))
        if self.acme_approval_id or self.acme_progress_certificate_id:
            return self._acme_project_invoice_action()
        if self.invoice_line_ids:
            raise UserError(_('Use an empty invoice draft to load a project source.'))
        project = self.acme_project_id
        project.check_access('read')
        if project.company_id != self.company_id:
            raise UserError(_('Use a project from the invoice company.'))
        if self.acme_invoice_payment_type == 'certificate':
            certificate = self.acme_invoice_pc_source_id
            if (not certificate or certificate.project_id != project or certificate.acme_certificate_type != 'progress'
                    or certificate.state != 'certified' or not certificate.invoice_id
                    or certificate.invoice_id.state == 'cancel'):
                raise UserError(_('Select an authorized Payment Certificate for this project.'))
            certificate.invoice_id.check_access('read')
            return certificate.invoice_id._acme_project_invoice_action()
        request = self.acme_invoice_advance_source_id
        if (self.acme_invoice_payment_type != 'advance' or not request or request.acme_project_id != project
                or request.acme_kind != 'advance' or request.request_status != 'approved'):
            raise UserError(_('Select a finally authorized Advance Request for this project.'))
        request.check_access('read')
        return request._acme_get_advance_certificate()._acme_load_advance_invoice(draft=self)

    def _acme_project_invoice_action(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'account.move',
                'view_mode': 'form', 'res_id': self.id}

    def _acme_invoice_fingerprint(self):
        self.ensure_one()
        return {
            'source': [self.acme_project_id.id, self.acme_approval_id.id, self.acme_progress_certificate_id.id,
                       self.acme_invoice_payment_type or False, self.acme_invoice_advance_source_id.id,
                       self.acme_invoice_pc_source_id.id],
            'terms': [self.company_id.id, self.partner_id.id, self.currency_id.id, self.journal_id.id,
                      self.fiscal_position_id.id, self.invoice_payment_term_id.id, str(self.invoice_date),
                      str(self.invoice_date_due), self.amount_untaxed, self.amount_total],
            'lines': [[line.display_type, line.name, line.product_id.id, line.product_uom_id.id, line.quantity,
                       line.price_unit, line.discount, sorted(line.tax_ids.ids), line.price_total,
                       line.account_id.id, line.analytic_distribution or {}]
                      for line in self.invoice_line_ids.sorted('id')],
        }

    def _acme_check_invoice_source(self):
        self.ensure_one()
        self._check_acme_invoice()
        if self.acme_approval_id:
            request = self.acme_approval_id
            certificate = self.acme_certificate_ids.filtered(lambda cert: cert.acme_advance_request_id == request)
            if not certificate or certificate.state != 'certified':
                raise UserError(_('Load the authorized Advance Request through its Advance Certificate.'))
            if self.currency_id.compare_amounts(self.amount_untaxed, request.acme_amount):
                raise UserError(_('The advance invoice untaxed amount must match the approved request.'))
        elif not (self.acme_progress_certificate_id.state == 'certified'
                  and self.acme_progress_certificate_id.invoice_id == self
                  and self.acme_progress_certificate_id.acme_pc_snapshot
                  == self.acme_progress_certificate_id._acme_pc_fingerprint(self)):
            if not self.env.user.has_group('base.group_system'):
                raise UserError(_('Load a Payment Certificate authorized for the current invoice figures.'))

    def _acme_invoice_signoff(self, role):
        self.ensure_one()
        self.check_access('write')
        self.env.cr.execute('UPDATE account_move SET id = id WHERE id = %s', [self.id])
        self.invalidate_recordset()
        if self.state != 'draft' or self.move_type != 'out_invoice' or not self.acme_project_id:
            raise UserError(_('Only a linked draft project customer invoice can be signed.'))
        assigned = self.env['acme.approval.position.assignment'].for_position(self.company_id, role).user_id
        is_admin = self.env.user.has_group('base.group_system')
        has_role_group = self.env.user.has_group('acme_project_core.group_acme_' + role) or is_admin
        if (assigned != self.env.user and not is_admin) or not has_role_group:
            raise AccessError(_('Only the designated company %s may sign this invoice.', role.upper()))
        self._acme_check_invoice_source()
        if not self.invoice_date:
            if is_admin:
                self.invoice_date = fields.Date.context_today(self)
            else:
                raise UserError(_('Set the invoice date before submitting it for sign-off.'))
        fingerprint = self._acme_invoice_fingerprint()
        if role == 'dm' and (not self.acme_invoice_odm_id or (self.acme_invoice_odm_id == self.env.user and not is_admin)
                             or (self.acme_invoice_snapshot != fingerprint and not is_admin)):
            raise UserError(_('A different ODM must approve the current invoice figures first.'))
        vals = {'acme_invoice_%s_id' % role: self.env.uid, 'acme_invoice_%s_at' % role: fields.Datetime.now()}
        if role == 'odm':
            vals.update(acme_invoice_snapshot=fingerprint, acme_invoice_dm_id=False, acme_invoice_dm_at=False)
        self.with_context(acme_invoice_signoff=_INVOICE_SIGNOFF).write(vals)
        self.message_post(body=_('Invoice signed by %s as %s.', self.env.user.display_name, role.upper()))

    def action_acme_invoice_odm_approve(self):
        return self._acme_invoice_signoff('odm')

    def action_acme_invoice_dm_authorize(self):
        return self._acme_invoice_signoff('dm')

    def _acme_clear_invoice_signoffs(self):
        if self.env.context.get('acme_invoice_post') is _INVOICE_POST:
            return
        signed = self.filtered(lambda move: move.state == 'draft' and move.acme_invoice_odm_id)
        if signed:
            signed.with_context(acme_invoice_signoff=_INVOICE_SIGNOFF).write(dict.fromkeys(_SIGNOFF_FIELDS, False))

    @api.constrains('acme_project_id', 'acme_approval_id', 'company_id', 'partner_id', 'currency_id', 'move_type')
    def _check_acme_invoice(self):
        for move in self:
            project = move.acme_project_id
            if not project:
                if move.acme_approval_id:
                    raise ValidationError(_('An advance invoice must carry its project.'))
                continue
            if move.move_type != 'out_invoice' or move.company_id != project.company_id or move.currency_id != project.acme_contract_currency_id or (move.partner_id.commercial_partner_id != project.partner_id.commercial_partner_id):
                raise ValidationError(_('Use a customer invoice matching the project company, customer and contract currency.'))
            approval = move.acme_approval_id
            if approval and (approval.acme_kind != 'advance' or approval.acme_project_id != project or approval.request_status != 'approved'):
                raise ValidationError(_('The invoice requires an approved advance request for this project.'))
            if approval:
                duplicates = self.search_count([('acme_approval_id', '=', approval.id), ('state', '!=', 'cancel'), ('id', '!=', move.id)])
                if move.state != 'cancel' and duplicates:
                    raise ValidationError(_('An advance approval can have only one noncancelled invoice.'))

    def _acme_project_customer_invoices(self):
        return self.filtered(lambda move: move.move_type == 'out_invoice' and (
            move.acme_is_project or move.acme_project_id or move.acme_approval_id or move.acme_progress_certificate_id
        ))

    def write(self, vals):
        if vals.get('state') == 'posted' and self.env.context.get('acme_invoice_post') is not _INVOICE_POST:
            unpublished = self.filtered(lambda move: move.state != 'posted')
            source_added = any(vals.get(field) for field in (
                'acme_is_project', 'acme_project_id', 'acme_approval_id', 'acme_progress_certificate_id',
            ))
            if unpublished._acme_project_customer_invoices() or (
                source_added and unpublished.filtered(lambda move: vals.get('move_type', move.move_type) == 'out_invoice')
            ):
                raise UserError(_('Use the Finance posting action for project invoices.'))
        internal = self.env.context.get('acme_invoice_signoff') is _INVOICE_SIGNOFF
        if _SIGNOFF_FIELDS.intersection(vals) and not internal:
            raise AccessError(_('Record invoice decisions using the approval actions.'))
        source_fields = {'acme_project_id', 'acme_approval_id', 'acme_is_project', 'acme_invoice_payment_type',
                         'acme_invoice_advance_source_id', 'acme_invoice_pc_source_id'}
        if source_fields.intersection(vals) and self.filtered(lambda move: move.state != 'draft'):
            raise UserError(_('Project invoice source details are locked after posting.'))
        if {'acme_project_id', 'acme_approval_id', 'move_type'}.intersection(vals):
            for move in self.filtered(lambda rec: rec.acme_approval_id or rec.acme_progress_certificate_id):
                for key in {'acme_project_id', 'acme_approval_id', 'move_type'}.intersection(vals):
                    current = move[key] if key == 'move_type' else move[key].id
                    if vals[key] != current:
                        raise UserError(_('Keep the original project and source on a linked invoice.'))
        result = super().write(vals)
        if not internal and _REVIEW_FIELDS.intersection(vals):
            self._acme_clear_invoice_signoffs()
        return result

    def _post(self, soft=True):
        project_moves = self.filtered(lambda move: move.state == 'draft')._acme_project_customer_invoices()
        is_admin = self.env.user.has_group('base.group_system')
        for move in project_moves.sorted('id'):
            self.env.cr.execute('UPDATE account_move SET id = id WHERE id = %s', [move.id])
            move.invalidate_recordset()
            if not self.env.user.has_group('account.group_account_user') and not is_admin:
                raise AccessError(_('Only Finance can post a project customer invoice.'))
            move._acme_check_invoice_source()
            if is_admin:
                if not move.invoice_date:
                    move.invoice_date = fields.Date.context_today(move)
                fp = move._acme_invoice_fingerprint()
                if not move.acme_invoice_odm_id:
                    move.with_context(acme_invoice_signoff=_INVOICE_SIGNOFF).write({
                        'acme_invoice_odm_id': self.env.uid,
                        'acme_invoice_odm_at': fields.Datetime.now(),
                        'acme_invoice_snapshot': fp,
                    })
                if not move.acme_invoice_dm_id:
                    move.with_context(acme_invoice_signoff=_INVOICE_SIGNOFF).write({
                        'acme_invoice_dm_id': self.env.uid,
                        'acme_invoice_dm_at': fields.Datetime.now(),
                        'acme_invoice_snapshot': fp,
                    })
            elif (not move.acme_invoice_odm_id or not move.acme_invoice_dm_id
                    or move.acme_invoice_snapshot != move._acme_invoice_fingerprint()):
                raise UserError(_('Obtain ODM approval and DM authorization for the current invoice before posting.'))
        self._check_acme_invoice()
        result = super(AccountMove, self.with_context(acme_invoice_post=_INVOICE_POST))._post(soft=soft)
        for move in project_moves.filtered(lambda invoice: invoice.state == 'posted'):
            if not is_admin and move.acme_invoice_snapshot != move._acme_invoice_fingerprint():
                raise UserError(_('Posting changed the signed invoice figures. Review and sign the draft again.'))
        return result


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        lines.filtered(lambda row: row.display_type in ('product', 'line_note', 'line_section')).move_id._acme_clear_invoice_signoffs()
        return lines

    def write(self, vals):
        if {'move_id', 'display_type', 'name', 'product_id', 'product_uom_id', 'quantity', 'price_unit',
            'discount', 'tax_ids', 'account_id', 'analytic_distribution'}.intersection(vals):
            self.filtered(lambda row: row.display_type in ('product', 'line_note', 'line_section')).move_id._acme_clear_invoice_signoffs()
        return super().write(vals)

    def unlink(self):
        moves = self.filtered(lambda row: row.display_type in ('product', 'line_note', 'line_section')).move_id
        result = super().unlink()
        moves._acme_clear_invoice_signoffs()
        return result


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    acme_can_open_project_invoice = fields.Boolean(compute='_compute_acme_can_open_project_invoice',
                                                   groups='account.group_account_invoice')

    def _acme_invoice_projects(self):
        projects = self.env['project.project']
        if not self or not projects.has_access('read'):
            return projects
        return projects.search([('acme_order_id', 'in', self.ids), ('company_id', 'in', self.company_id.ids)])

    @api.depends('state')
    def _compute_acme_can_open_project_invoice(self):
        projects = self.filtered(lambda order: order.state == 'sale')._acme_invoice_projects()
        orders = projects.acme_order_id
        for order in self:
            order.acme_can_open_project_invoice = order in orders

    def action_acme_open_project_invoice(self):
        self.ensure_one()
        self.check_access('read')
        self.env['account.move'].check_access('create')
        project = self._acme_invoice_projects()
        if self.state != 'sale' or len(project) != 1 or project.company_id != self.company_id:
            raise UserError(_('Use a confirmed Sales Order linked to one project in the same company.'))
        return {
            'type': 'ir.actions.act_window', 'name': _('Project Invoice'), 'res_model': 'account.move',
            'view_mode': 'form', 'views': [(self.env.ref('account.view_move_form').id, 'form')],
            'context': dict(self.env.context, default_move_type='out_invoice', default_company_id=self.company_id.id,
                            default_partner_id=(self.partner_invoice_id or project.partner_id).id,
                            default_currency_id=project.acme_contract_currency_id.id, default_acme_is_project=True,
                            default_acme_project_id=project.id),
        }
