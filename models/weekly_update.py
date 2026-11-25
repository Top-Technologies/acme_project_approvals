from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError


class WeeklyProjectUpdate(models.Model):
    _inherit = 'project.update'

    acme_has_planned_progress = fields.Boolean(string='Planned Progress Available')
    acme_planned_progress = fields.Float(string='Planned Progress (%)')

    @api.constrains('progress', 'acme_has_planned_progress', 'acme_planned_progress')
    def _check_acme_progress(self):
        for update in self:
            if not 0 <= update.progress <= 100 or (
                update.acme_has_planned_progress and not 0 <= update.acme_planned_progress <= 100
            ):
                raise ValidationError(_('Actual and planned progress must be between 0 and 100.'))

    def action_acme_request_weekly_review(self):
        self.ensure_one()
        self.check_access('write')
        project = self.project_id
        if project.user_id != self.env.user:
            raise AccessError(_('Only the assigned PM can request the weekly ODM review.'))
        odm = self.env['acme.approval.position.assignment'].for_position(project.company_id, 'odm').user_id
        if not odm.active or not odm.has_group('acme_project_core.group_acme_odm') or project.company_id not in odm.company_ids:
            raise UserError(_('Configure an active ODM with access to the project company.'))
        odm_update = self.with_user(odm).with_context(allowed_company_ids=project.company_id.ids)
        if not odm_update.has_access('read'):
            raise UserError(_('The assigned ODM must be able to read this project update.'))
        self.env.cr.execute('UPDATE project_update SET id = id WHERE id = %s', [self.id])
        self.invalidate_recordset()
        activity_type = self.env.ref('acme_project_approvals.activity_weekly_project_review')
        if not self.activity_ids.filtered(lambda activity: activity.active and activity.activity_type_id == activity_type):
            self.activity_schedule(
                'acme_project_approvals.activity_weekly_project_review', user_id=odm.id,
                summary=_('Review weekly project status'),
            )
