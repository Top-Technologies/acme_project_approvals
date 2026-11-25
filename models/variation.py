import math

from markupsafe import Markup

from odoo import api, Command, fields, models, _
from odoo.exceptions import AccessError, UserError
from odoo.tools import html2plaintext, is_html_empty
from odoo.addons.acme_project_core.models.boq_revision import _BOQ_TOKEN, snapshot


_VARIATION_TOKEN = object()
_AUDIT = {'acme_variation_snapshot', 'acme_variation_route', 'acme_variation_revision_id'}


class ProjectBoqVariation(models.Model):
    _inherit = 'project.project'

    acme_boq_variation_editor = fields.Boolean(compute='_compute_boq_variation_editor')
    acme_boq_proposal_line_ids = fields.One2many('acme.project.boq', 'project_id', string='Proposed BOQ Lines')

    @api.depends('user_id')
    @api.depends_context('uid')
    def _compute_boq_variation_editor(self):
        for project in self:
            project.acme_boq_variation_editor = project.user_id == self.env.user

    def action_acme_edit_boq(self):
        self.ensure_one()
        if self.user_id != self.env.user:
            raise AccessError(_('Only the assigned Project Manager may edit the BOQ.'))
        return super().action_acme_edit_boq()

    def action_acme_submit_boq(self):
        self._acme_lock_boq()
        self._acme_require_requester()
        if self.user_id != self.env.user:
            raise AccessError(_('Only the assigned Project Manager may submit the BOQ amendment.'))
        reason = (self.acme_boq_reason or '').strip()
        if not reason:
            raise UserError(_('Enter the reason for the BOQ change.'))
        categories = self.env['approval.category'].search([
            ('acme_kind', '=', 'variation'), ('company_id', '=', self.company_id.id), ('active', '=', True),
        ])
        if len(categories) != 1:
            raise UserError(_('Configure exactly one active Variation approval category for this company.'))
        rows = []
        for line in self.acme_boq_ids.sorted('id'):
            baseline = line._acme_variation_snapshot()
            original = baseline['boq']
            proposed = snapshot(line, proposed=True)
            if any(proposed[key] != original[key] for key in original if key != 'quantity'):
                raise UserError(_('Edit BOQ changes quantities only. Discard this draft and use a Variation for scope or price changes.'))
            quantity = proposed['quantity']
            if not math.isfinite(quantity) or quantity < 0:
                raise UserError(_('Proposed quantities must be finite nonnegative numbers.'))
            rows.append({'line_id': line.id, 'baseline': baseline, 'proposed': quantity})
        changed = [row for row in rows if row['proposed'] != row['baseline']['boq']['quantity']]
        if not changed:
            raise UserError(_('There are no BOQ quantity changes to submit.'))
        request = self.env['approval.request'].create({
            'category_id': categories.id, 'acme_project_id': self.id, 'request_owner_id': self.env.uid,
            'name': _('BOQ Amendment: %s', self.display_name),
            'reason': Markup('<p>%s</p>') % reason,
        })
        for row in changed:
            source = self.env['sale.order.line'].browse(row['baseline']['source'] or [])
            if source:
                row['baseline']['commercial'] = request._acme_addendum_source_terms(source)
        edm = self.env['acme.approval.position.assignment'].for_position(self.company_id, 'edm').user_id
        if (not edm.active or self.company_id not in edm.company_ids
                or not edm.has_group('acme_project_core.group_acme_edm')
                or not self.with_user(edm).with_context(allowed_company_ids=self.company_id.ids).has_access('read')):
            raise UserError(_('Configure an active EDM with access to this project for consultation.'))
        value = sum((row['proposed'] - row['baseline']['boq']['quantity']) * row['baseline']['selling_rate']
                    for row in changed)
        request._acme_submit_variation({'lines': rows}, value)
        self.with_context(acme_boq_token=_BOQ_TOKEN).write({
            'acme_boq_state': 'approved', 'acme_boq_reason': False,
        })
        self.activity_schedule(
            'mail.mail_activity_data_todo', user_id=edm.id, summary=_('Consult on BOQ Amendment'),
            note=(Markup('<p>%s</p>') % _('Record EDM consultation here. Authorization remains in Approvals.'))
            + request.reason + request.acme_variation_batch_details,
        )
        self.message_post(body=_('BOQ amendment %s submitted for approval. The effective BOQ is unchanged.', request.display_name))
        return {'type': 'ir.actions.act_window', 'res_model': 'approval.request', 'res_id': request.id, 'view_mode': 'form'}


class VariationRequest(models.Model):
    _inherit = 'approval.request'

    acme_variation_line_id = fields.Many2one('acme.project.boq', check_company=True, ondelete='restrict', copy=False)
    acme_variation_new_source_id = fields.Many2one('sale.order.line', string='New BOQ Line from Contract Order',
                                                  check_company=True, ondelete='restrict', copy=False)
    acme_variation_order_id = fields.Many2one(related='acme_project_id.acme_order_id')
    acme_variation_quantity = fields.Float(string='Proposed Quantity')
    acme_variation_change_rate = fields.Boolean(string='Change Selling Rate')
    acme_variation_rate = fields.Monetary(string='Proposed Untaxed Selling Rate', currency_field='acme_currency_id')
    acme_variation_description = fields.Char(string='Proposed Scope Description')
    acme_variation_value = fields.Monetary(compute='_compute_variation_value', currency_field='acme_currency_id')
    acme_variation_task_ids = fields.Many2many('project.task', 'acme_variation_task_rel', 'request_id', 'task_id',
                                              string='Affected Schedule Tasks', copy=False)
    acme_variation_current_quantity = fields.Float(string='Current Quantity', compute='_compute_variation_current_quantity')
    acme_variation_batch_details = fields.Html(compute='_compute_variation_batch_details')
    acme_variation_snapshot = fields.Json(readonly=True, copy=False)
    acme_variation_route = fields.Json(readonly=True, copy=False)
    acme_variation_revision_id = fields.Many2one('acme.project.boq.revision', readonly=True, copy=False, ondelete='restrict')

    @api.depends('acme_variation_snapshot')
    def _compute_variation_batch_details(self):
        for request in self:
            rows = (request.acme_variation_snapshot or {}).get('lines', [])
            request.acme_variation_batch_details = False
            if rows:
                labels = (_('Reference'), _('Description'), _('Original Quantity'), _('Proposed Quantity'))
                header = Markup('').join(Markup('<th>%s</th>') % label for label in labels)
                body = Markup('').join(
                    Markup('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>') % (
                        row['baseline']['boq']['reference'] or '', row['baseline']['boq']['name'],
                        row['baseline']['boq']['quantity'], row['proposed'],
                    ) for row in rows
                )
                request.acme_variation_batch_details = Markup(
                    '<table class="table table-sm"><thead><tr>%s</tr></thead><tbody>%s</tbody></table>'
                ) % (header, body)

    @api.depends('acme_variation_line_id.quantity', 'acme_variation_snapshot')
    def _compute_variation_current_quantity(self):
        for request in self:
            if (request.acme_variation_snapshot or {}).get('boq'):
                request.acme_variation_current_quantity = request.acme_variation_snapshot['boq']['quantity']
            else:
                request.acme_variation_current_quantity = request.acme_variation_line_id.quantity

    @api.onchange('acme_variation_line_id')
    def _onchange_variation_line(self):
        self.acme_variation_quantity = self.acme_variation_line_id.quantity
        self.acme_variation_rate = self.acme_variation_line_id.selling_rate

    @api.onchange('acme_variation_new_source_id')
    def _onchange_variation_source(self):
        if self.acme_variation_new_source_id:
            source = self.acme_variation_new_source_id
            self.acme_variation_line_id = False
            self.acme_variation_change_rate = True
            self.acme_variation_quantity = source.product_uom_qty
            self.acme_variation_rate = source.price_subtotal / source.product_uom_qty if source.product_uom_qty else 0

    @api.depends('acme_variation_snapshot', 'acme_variation_line_id.quantity',
                 'acme_variation_line_id.selling_rate', 'acme_variation_quantity',
                 'acme_variation_change_rate', 'acme_variation_rate', 'acme_currency_id')
    def _compute_variation_value(self):
        for request in self:
            source = request.acme_variation_snapshot or {}
            quantity = source.get('boq', {}).get('quantity', request.acme_variation_line_id.quantity)
            rate = source.get('selling_rate', request.acme_variation_line_id.selling_rate)
            proposed = request.acme_variation_rate if request.acme_variation_change_rate else rate
            if 'lines' in source:
                value = sum((row['proposed'] - row['baseline']['boq']['quantity']) * row['baseline']['selling_rate']
                            for row in source['lines'])
            else:
                value = request.acme_variation_quantity * proposed - quantity * rate
            request.acme_variation_value = request.acme_currency_id.round(value) if request.acme_currency_id else value

    @api.model_create_multi
    def create(self, vals_list):
        if any(_AUDIT.intersection(vals) for vals in vals_list) or any(
            self.env.context.get(f'default_{field}') for field in _AUDIT
        ):
            raise AccessError(_('Variation audit fields are generated by the approval workflow.'))
        return super().create(vals_list)

    def write(self, vals):
        if self.env.context.get('acme_variation_token') is not _VARIATION_TOKEN:
            if _AUDIT.intersection(vals):
                raise AccessError(_('Variation audit fields are generated by the approval workflow.'))
            locked = {'acme_variation_line_id', 'acme_variation_quantity', 'acme_project_id', 'reason',
                      'category_id', 'request_owner_id', 'approver_ids', 'acme_variation_change_rate',
                      'acme_variation_rate', 'acme_variation_description', 'acme_variation_task_ids',
                      'acme_variation_new_source_id'}
            if locked.intersection(vals) and self.filtered('acme_variation_snapshot'):
                raise UserError(_('Submitted variations are locked. Create a new request for changes.'))
        return super().write(vals)

    def action_confirm(self):
        self.ensure_one()
        if self.acme_kind != 'variation':
            return super().action_confirm()
        self.check_access('write')
        self.env.cr.execute('UPDATE approval_request SET id = id WHERE id = %s', [self.id])
        self.invalidate_recordset()
        project = self.acme_project_id
        if not project or project.user_id != self.env.user or self.request_owner_id != self.env.user:
            raise AccessError(_('Only the assigned Project Manager may submit this variation.'))
        if self.acme_variation_snapshot or self.request_status != 'new':
            raise UserError(_('Only a new variation can be submitted.'))
        line = self.acme_variation_line_id
        project._acme_lock_boq()
        source = self.acme_variation_new_source_id
        if not project.acme_boq_initialized or bool(line) == bool(source) or (line and line.project_id != project):
            raise UserError(_('Select one existing project BOQ line or one new line from the confirmed contract.'))
        baseline = project._acme_new_variation_baseline(source) if source else line._acme_variation_snapshot()
        if source and (not self.acme_variation_change_rate or self.acme_variation_quantity <= 0):
            raise UserError(_('A new line requires a positive quantity and its proposed selling rate.'))
        quantity = self.acme_variation_quantity
        rate = self.acme_variation_rate if self.acme_variation_change_rate else line.selling_rate
        description = (self.acme_variation_description or '').strip() or line.name
        if (not math.isfinite(quantity) or quantity < 0 or not math.isfinite(rate) or rate < 0
                or (quantity, rate, description) == (line.quantity, line.selling_rate, line.name)
                or is_html_empty(self.reason)):
            raise UserError(_('Enter a reason and a valid scope, quantity, or selling-rate change.'))
        if self.acme_variation_task_ids.filtered(lambda task: task.project_id != project):
            raise UserError(_('Affected schedule tasks must belong to this project.'))
        if line.source_line_id:
            baseline['commercial'] = self._acme_addendum_source_terms(line.source_line_id)
        return self._acme_submit_variation(baseline, self.acme_variation_value)

    def _acme_submit_variation(self, baseline, value):
        project = self.acme_project_id
        if not self.acme_currency_id:
            raise UserError(_('Set the contract currency before submitting this variation.'))
        value = self.acme_currency_id.round(value)
        if value < 0 and not project.company_id.acme_variation_route_reductions_by_absolute:
            raise UserError(_('Configure the company routing basis for negative variation amounts before submitting.'))
        rule = self.env['acme.approval.authority'].matching_rule(
            project.company_id, self.acme_currency_id, abs(value), 'variation',
        )
        if rule.approver_position != 'odm' or rule.authorizer_position not in ('dm', 'dgm'):
            raise UserError(_('Configure Variation authority with ODM review and DM or DGM authorization.'))
        if self.category_id.approval_minimum != 2 or not self.category_id.approver_sequence:
            raise UserError(_('Configure Variation with two minimum approvals and Approvers Sequence enabled.'))
        assignments = self.env['acme.approval.position.assignment']
        positions = ['odm', 'dm'] + (['dgm'] if rule.authorizer_position == 'dgm' else [])
        route = {}
        for position in positions:
            user = assignments.for_position(project.company_id, position).user_id
            if not user.active or project.company_id not in user.company_ids or not user.has_group(
                f'acme_project_core.group_acme_{position}'
            ):
                raise UserError(_('Configure an active %(position)s with company access.', position=position.upper()))
            route[position] = user.id
        if len(set(route.values()) | {self.env.uid}) != len(positions) + 1:
            raise UserError(_('The PM and designated approvers must be different users.'))
        request = self.with_context(acme_variation_token=_VARIATION_TOKEN)
        request.write({
            'acme_variation_snapshot': baseline,
            'acme_variation_route': route,
            'approver_ids': [Command.clear(), *[
                Command.create({'user_id': route[position], 'required': True, 'sequence': index * 10})
                for index, position in enumerate(positions, start=1)
            ]],
        })
        return super(VariationRequest, request).action_confirm()

    def action_approve(self, approver=None):
        if not self.filtered(lambda request: request.acme_kind == 'variation'):
            return super().action_approve(approver=approver)
        self.ensure_one()
        self.check_access('write')
        self.env.cr.execute('UPDATE approval_request SET id = id WHERE id = %s', [self.id])
        self.invalidate_recordset()
        route = self.acme_variation_route or {}
        if self.acme_variation_revision_id:
            return
        positions = [position for position in ('odm', 'dm', 'dgm') if position in route]
        states = {row.user_id.id: row.status for row in self.approver_ids}
        position = next((role for role in positions if states.get(route[role]) != 'approved'), False)
        if (not position or states.get(route[position]) != 'pending' or self.request_status != 'pending'
                or self.env.uid != route.get(position)) or not self.env.user.has_group(
            f'acme_project_core.group_acme_{position}'
        ):
            raise AccessError(_('Only the next designated authority may approve this variation.'))
        request = self.with_context(acme_variation_token=_VARIATION_TOKEN)
        result = super(VariationRequest, request).action_approve()
        if position == positions[-1]:
            if request.request_status != 'approved':
                raise UserError(_('The variation did not reach final approval.'))
            if 'lines' in request.acme_variation_snapshot:
                rows = request.acme_variation_snapshot['lines']
                updates = [
                    (self.env['acme.project.boq'].browse(row['line_id']),
                     {key: value for key, value in row['baseline'].items() if key != 'commercial'},
                     row['proposed'], {})
                    for row in rows if row['proposed'] != row['baseline']['boq']['quantity']
                ]
                request.acme_variation_revision_id = request.acme_project_id._acme_apply_boq_variations(
                    updates, html2plaintext(request.reason), request.request_owner_id, request.date_confirmed,
                )
                return result
            changes = {}
            if request.acme_variation_change_rate:
                changes['selling_rate'] = request.acme_variation_rate
            if (request.acme_variation_description or '').strip():
                changes['name'] = request.acme_variation_description.strip()
            source = request.acme_variation_new_source_id
            apply_revision = (request.acme_project_id._acme_apply_new_line_variation if source
                              else request.acme_project_id._acme_apply_quantity_variation)
            baseline = {key: value for key, value in request.acme_variation_snapshot.items() if key != 'commercial'}
            revision = apply_revision(
                source or request.acme_variation_line_id, baseline, request.acme_variation_quantity,
                html2plaintext(request.reason), request.request_owner_id, request.date_confirmed, changes=changes,
            )
            request.acme_variation_revision_id = revision
        return result

    def _action_force_approval(self):
        if self.filtered(lambda request: request.acme_kind == 'variation'):
            raise UserError(_('Variation requires sequential ODM and DM approval.'))
        return super()._action_force_approval()


class VariationApprover(models.Model):
    _inherit = 'approval.approver'

    def _check_variation_route(self):
        if self.env.context.get('acme_variation_token') is not _VARIATION_TOKEN and self.request_id.filtered('acme_variation_snapshot'):
            raise AccessError(_('Variation approval routing is locked.'))

    @api.model_create_multi
    def create(self, vals_list):
        rows = super().create(vals_list)
        rows._check_variation_route()
        return rows

    def write(self, vals):
        if {'request_id', 'user_id', 'sequence', 'required'}.intersection(vals) or vals.get('status') == 'approved':
            self._check_variation_route()
            if vals.get('request_id') and self.env['approval.request'].browse(vals['request_id']).acme_variation_snapshot:
                raise AccessError(_('Variation approval routing is locked.'))
        if 'status' in vals and self.request_id.filtered('acme_variation_revision_id'):
            raise UserError(_('An applied variation is historical. Submit a new variation for corrections.'))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_variation_route(self):
        self._check_variation_route()


class ProjectVariationTask(models.Model):
    _inherit = 'project.task'

    acme_task_variation_ids = fields.Many2many('approval.request', 'acme_variation_task_rel',
                                              'task_id', 'request_id', string='Related Variations', readonly=True, copy=False)

    def write(self, vals):
        if {'acme_start_date', 'acme_end_date', 'planned_date_begin', 'date_deadline'}.intersection(vals):
            affected = self.filtered(lambda task: task.acme_task_variation_ids.filtered('acme_variation_revision_id'))
            if affected:
                affected.project_id._acme_check_preparation_editor()
                for task in affected:
                    task.message_post(body=_('Schedule edited with authorized variations on record: %s',
                                             ', '.join(task.acme_task_variation_ids.filtered('acme_variation_revision_id').mapped('display_name'))))
        if 'acme_task_variation_ids' in vals:
            raise AccessError(_('Link schedule tasks through the Variation request.'))
        return super().write(vals)
