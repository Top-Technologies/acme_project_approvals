from odoo import Command
from odoo.exceptions import UserError, ValidationError
from odoo.tests import new_test_user
from odoo.tests.common import tagged

from odoo.addons.acme_project_core.tests.test_project_flow import ProjectFlowCase


@tagged('post_install', '-at_install')
class TestProjectApprovals(ProjectFlowCase):

    def setUp(self):
        super().setUp()
        self.dm = new_test_user(self.env, login='acme_approval_dm', groups='approvals.group_approval_user,account.group_account_invoice', company_id=self.env.company.id)
        self.dgm = new_test_user(self.env, login='acme_approval_dgm', groups='approvals.group_approval_user', company_id=self.env.company.id)
        self.bond_category = self.env.ref('acme_project_approvals.category_bond')
        self.advance_category = self.env.ref('acme_project_approvals.category_advance')
        self.bond_category.write({
            'approval_minimum': 1,
            'approver_ids': [Command.clear(), Command.create({'user_id': self.dm.id, 'required': True, 'sequence': 10})],
        })
        self.advance_category.write({
            'approval_minimum': 2,
            'approver_sequence': True,
            'approver_ids': [
                Command.clear(),
                Command.create({'user_id': self.dm.id, 'required': True, 'sequence': 10}),
                Command.create({'user_id': self.dgm.id, 'required': True, 'sequence': 20}),
            ],
        })

    def _request(self, kind, project=None, amount=None, **values):
        category = self.bond_category if kind == 'bond' else self.advance_category
        project = project or self.project
        vals = {
            'name': '%s request' % kind.title(),
            'category_id': category.id,
            'request_owner_id': self.env.user.id,
            'acme_project_id': project.id,
            'acme_amount': amount if amount is not None else (project.acme_performance_bond_amount if kind == 'bond' else 10000),
        }
        vals.update(values)
        return self.env['approval.request'].create(vals)

    def _approve_advance(self, request):
        request.action_confirm()
        request.with_user(self.dm).action_approve()
        request.with_user(self.dgm).action_approve()
        self.assertEqual(request.request_status, 'approved')

    def test_bond_without_sales_order_and_submitted_content_locked(self):
        project = self.project.copy({
            'name': 'Bond Project Without SO', 'acme_order_id': False,
            'acme_contract_amount': 500000, 'acme_contract_total': 575000,
            'acme_bond_basis': 'total',
        })
        request = self._request(
            'bond', project=project, amount=57500,
            acme_addressee='Commercial Bank Guarantee Desk',
            acme_addressee_address='Guarantees Department\nAddis Ababa',
        )
        request.action_confirm()
        self.assertEqual(request.request_status, 'pending')
        with self.assertRaises(UserError):
            request.write({'acme_addressee': 'Changed after submission'})
        with self.assertRaises(UserError):
            request.write({'name': 'Changed subject'})

    def test_invalid_project_company_and_incorrect_bond_amount_rejected(self):
        other_company = self.env['res.company'].create({'name': 'Other ACME Company'})
        invalid_project = self.project.copy({'name': 'Wrong Company Project', 'company_id': other_company.id, 'acme_order_id': False})
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self._request('bond', project=invalid_project)
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self._request('bond', amount=self.project.acme_performance_bond_amount + 1)

    def test_company_aware_category_selection(self):
        other_company = self.env['res.company'].create({'name': 'Selection Company'})
        other_project = self.project.copy({
            'name': 'Other Company Project',
            'company_id': other_company.id,
            'acme_order_id': False,
        })
        other_bond = self.env['approval.category'].create({
            'name': 'Other Company Bond',
            'company_id': other_company.id,
            'acme_kind': 'bond',
            'approval_minimum': 1,
        })
        self.assertEqual(
            self.project.action_acme_bond_request()['context']['default_category_id'],
            self.bond_category.id,
        )
        self.assertEqual(
            other_project.action_acme_bond_request()['context']['default_category_id'],
            other_bond.id,
        )

    def test_missing_and_ambiguous_company_category_rejected(self):
        other_company = self.env['res.company'].create({'name': 'Category Error Company'})
        other_project = self.project.copy({
            'name': 'Category Error Project',
            'company_id': other_company.id,
            'acme_order_id': False,
        })
        with self.assertRaises(UserError):
            other_project.action_acme_advance_request()
        self.env['approval.category'].create({
            'name': 'Bond One', 'company_id': other_company.id,
            'acme_kind': 'bond', 'approval_minimum': 1,
        })
        self.env['approval.category'].create({
            'name': 'Bond Two', 'company_id': other_company.id,
            'acme_kind': 'bond', 'approval_minimum': 1,
        })
        with self.assertRaises(UserError):
            other_project.action_acme_bond_request()

    def test_cross_company_category_request_rejected(self):
        other_company = self.env['res.company'].create({'name': 'Cross Company'})
        other_project = self.project.copy({
            'name': 'Cross Company Project',
            'company_id': other_company.id,
            'acme_order_id': False,
        })
        with self.assertRaises(ValidationError), self.cr.savepoint():
            self._request('bond', project=other_project)

    def test_bond_report_uses_explicit_addressee(self):
        request = self._request(
            'bond', acme_addressee='Awash Bank Guarantee Unit',
            acme_addressee_address='Churchill Road\nAddis Ababa',
            acme_beneficiary='ACME Contract Beneficiary',
        )
        html, _ = self.env.ref('acme_project_approvals.report_project_request')._render_qweb_html(request.ids)
        rendered = html.decode()
        self.assertIn('Awash Bank Guarantee Unit', rendered)
        self.assertIn('Churchill Road', rendered)
        self.assertIn('ACME Contract Beneficiary', rendered)

    def test_missing_approvers_and_self_approval_rejected(self):
        request = self._request('bond')
        self.bond_category.approver_ids = [Command.clear()]
        with self.assertRaises(UserError):
            request.action_confirm()
        self.bond_category.approver_ids = [Command.create({'user_id': self.env.user.id, 'required': True, 'sequence': 10})]
        request = self._request('bond')
        with self.assertRaises(UserError):
            request.action_confirm()

    def test_advance_approval_order(self):
        request = self._request('advance')
        request.action_confirm()
        with self.assertRaises(ValidationError):
            request.with_user(self.dgm).action_approve()
        request.with_user(self.dm).action_approve()
        self.assertEqual(request.approver_ids.filtered(lambda a: a.user_id == self.dgm).status, 'pending')
        request.with_user(self.dgm).action_approve()
        self.assertEqual(request.request_status, 'approved')

    def test_unapproved_advance_cannot_invoice(self):
        request = self._request('advance')
        with self.assertRaises(UserError):
            request.with_user(self.dm).action_acme_create_advance_invoice()

    def test_approved_advance_requires_matching_confirmed_order(self):
        project = self.project.copy({
            'name': 'Advance Project Without SO', 'acme_order_id': False,
            'acme_contract_amount': 500000, 'acme_contract_total': 500000,
        })
        request = self._request('advance', project=project)
        self._approve_advance(request)
        with self.assertRaises(UserError):
            request.with_user(self.dm).action_acme_create_advance_invoice()

    def test_repeated_invoice_action_opens_existing_active_invoice(self):
        request = self._request('advance')
        self._approve_advance(request)
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.project.partner_id.id,
            'company_id': self.project.company_id.id,
            'currency_id': self.project.acme_contract_currency_id.id,
            'acme_project_id': self.project.id, 'acme_approval_id': request.id,
        })
        action = request.with_user(self.dm).action_acme_create_advance_invoice()
        self.assertEqual(action['res_id'], invoice.id)
        self.assertEqual(len(request.acme_invoice_ids.filtered(lambda move: move.state != 'cancel')), 1)
