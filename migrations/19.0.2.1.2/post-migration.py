def migrate(cr, version):
    if not version:
        return
    cr.execute('''
        UPDATE approval_request AS request
           SET acme_contract_reference = project.acme_contract_reference
          FROM project_project AS project
         WHERE request.acme_project_id = project.id
           AND request.acme_contract_reference IS NULL
    ''')
