from io import BytesIO

import xlsxwriter

from odoo import http
from odoo.http import content_disposition, request

from .boq_workbook import write_project_boq
from .ipc_workbook import write_client_workbook


_XLSX_OPTIONS = {'in_memory': True, 'strings_to_formulas': False, 'strings_to_urls': False}


def _xlsx_response(output, filename):
    return request.make_response(output.getvalue(), headers=[
        ('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
        ('Content-Disposition', content_disposition(filename)),
    ])


class IPCExport(http.Controller):

    @http.route(['/acme/ipc/<int:certificate_id>/xlsx', '/acme/ipc/<int:certificate_id>/3in1.xlsx'], type='http', auth='user', methods=['GET'])
    def download(self, certificate_id):
        certificate = request.env['acme.project.certificate'].browse(certificate_id).exists()
        if not certificate:
            return request.not_found()
        certificate.check_access('read')
        output = BytesIO()
        ipc = certificate._acme_ipc_outputs()
        with xlsxwriter.Workbook(output, _XLSX_OPTIONS) as book:
            write_client_workbook(book, ipc)
        project_code = certificate.project_id.acme_project_sequence or certificate.project_id.id
        cert_num = certificate.number or certificate.id
        filename = f'3-in-1-{project_code}-{cert_num}.xlsx'
        return _xlsx_response(output, filename)


class ProjectBOQExport(http.Controller):

    @http.route('/acme/project/<int:project_id>/boq.xlsx', type='http', auth='user', methods=['GET'])
    def download_boq(self, project_id):
        project = request.env['project.project'].browse(project_id).exists()
        if not project:
            return request.not_found()
        project._acme_check_boq_export()
        lines = project.acme_boq_ids
        lines.check_access('read')
        output = BytesIO()
        with xlsxwriter.Workbook(output, _XLSX_OPTIONS) as book:
            write_project_boq(book, project, lines)
        return _xlsx_response(output, 'Project-BOQ-%s.xlsx' % project.id)
