from .boq_workbook import _description_height


def write_client_workbook(book, data):
    cover = data['cover']
    values = [row[1] for row in cover]
    round_name, project, reference, code, client, site, period, currency = values[:8]
    meta = data.get('client_excel', {})
    quantity = book.add_format({'border': 1, 'num_format': '#,##0.############'})
    rate = book.add_format({'border': 1, 'num_format': '#,##0.00######'})
    number = book.add_format({'border': 1, 'num_format': '#,##0.00;[Red](#,##0.00);0.00'})
    cell = book.add_format({'border': 1, 'text_wrap': True, 'valign': 'top'})
    head = book.add_format({'border': 1, 'bold': True, 'align': 'center', 'valign': 'vcenter',
                            'text_wrap': True, 'bg_color': '#e7edf3'})
    title = book.add_format({'bold': True, 'font_size': 16, 'align': 'center', 'valign': 'vcenter'})
    label = book.add_format({'bold': True, 'text_wrap': True})
    wrapped = book.add_format({'text_wrap': True, 'valign': 'vcenter'})

    def sheet(name, widths, portrait=False):
        ws = book.add_worksheet(name)
        ws.set_paper(9)
        ws.hide_gridlines(2)
        ws.set_footer('&RPage &P of &N')
        ws.set_portrait() if portrait else ws.set_landscape()
        ws.fit_to_pages(1, 1 if portrait else 0)
        ws.set_margins(.25, .25, .35, .35)
        for col, width in enumerate(widths):
            ws.set_column(col, col, width)
        return ws

    def write_row(ws, row, cells):
        for col, val in enumerate(cells):
            ws.write(row, col, val, number if isinstance(val, (float, int)) else cell)

    def signatures(ws, row, width, roles):
        for index, (role, person, signed_on) in enumerate(roles):
            first = index * width // len(roles)
            last = (index + 1) * width // len(roles) - 1
            for offset, text in enumerate((role, 'Name: ' + (person or '________________'),
                                          'Signature: __________________', 'Date: ' + (signed_on or '____________'))):
                ws.merge_range(row + offset, first, row + offset, last, text, label if not offset else wrapped)

    ipc = sheet('IPC', (14.7, 17.4, 14.7, 12, 12, 23, 21.1, 16.1), True)
    summary = sheet('Summary', (15.4, 39.4, 14.6, 12.6, 15, 17))
    detail = sheet('Detailed BOQ', (8.9, 56.9, 8, 11, 13, 12, 12, 12, 15, 15, 15, 15))
    ipc.merge_range('A3:H3', f'Payment Certificate - {round_name.split()[-1]}', title)
    ipc.set_row(2, 29)
    for row, (caption, value) in enumerate((('Project Name', project), ('Project Location', site),
            ('Client', client), ('Contractor', meta.get('contractor', '')), ('Contract Reference', reference),
            ('Project Code', code), ('Period', period), ('Currency', currency)), 4):
        ipc.merge_range(row, 0, row, 2, caption, label)
        ipc.merge_range(row, 3, row, 7, value, wrapped)
        ipc.set_row(row, 31 if row == 4 else 16)
    ipc.merge_range('A13:D13', 'Certification Date: ' + cover[17][2][:10], wrapped)
    ipc.merge_range('E13:G13', 'Contract Value', head)
    ipc.write('H13', currency, head)
    contract_rows = [('Signed Contract Value', meta.get('contract')),
                     ('Authorized Variations', meta.get('variations')),
                     ('Advance %', meta.get('advance_percent'))]
    for row, (caption, value) in enumerate(contract_rows, 13):
        ipc.merge_range(row, 4, row, 6, caption, cell)
        ipc.write(row, 7, value, number)
    ipc.merge_range('A23:D23', 'Previous Payments', head)
    ipc.merge_range('E23:H23', 'Current Payment Calculation', head)
    prior = sum(row[8] for row in data['detail'])
    to_date = sum(row[12] for row in data['detail'])
    ipc.merge_range('A29:C29', 'Purpose', head)
    ipc.write('D29', 'Service Sum', head)
    ipc.merge_range('A30:C30', 'Previous executed value', cell)
    ipc.write('D30', prior, number)
    ipc.set_row(29, 28)
    rows = [('Gross Work to Date', to_date), ('Current Work Value', values[8]), ('Rebate', meta.get('rebate')),
            ('Retention', values[10]), ('Advance Recovery', values[9]), ('VAT', values[11]),
            ('Withholding', values[12]), ('Net Payable', values[13])]
    for i, (caption, value) in enumerate(rows):
        row = 41 if caption == 'Net Payable' else 24 + i
        ipc.merge_range(row, 4, row, 6, caption, head if caption == 'Net Payable' else cell)
        ipc.write(row, 7, value, number)
    ipc.merge_range('A44:H45', meta.get('amount_words', ''), wrapped)
    signatures(ipc, 47, 8, [(cover[i][0], cover[i][1], cover[i][2][:10]) for i in (14, 16, 17)])
    ipc.print_area(0, 0, 51, 7)

    for ws, last in ((summary, 5), (detail, 11)):
        for row, (caption, value) in enumerate((('Project Name', project), ('Employer', client),
                ('Contractor', meta.get('contractor', '')), ('Location', site))):
            ws.write(row, 0, caption, label)
            ws.merge_range(row, 1, row, last, value, wrapped)
            if row == 0:
                ws.set_row(row, 26)
    summary.merge_range('A6:F6', 'Grand Summary', title)
    summary.write_row(6, 0, ['No.', 'Description', f'Contract Amount ({currency})',
        f'Previous Executed Amount ({currency})', f'Current Executed Amount ({currency})',
        f'To-Date Executed Amount ({currency})'], head)
    summary.set_row(6, 36)
    for index, row in enumerate(data['summary'], 1):
        write_row(summary, 6 + index, [index, *row[:5]])
        summary.set_row(6 + index, 27)
    end = 7 + len(data['summary'])
    sums = [sum(row[col] for row in data['summary']) for col in range(1, 5)]
    write_row(summary, end, ['', 'Total', *sums])
    rebate = meta.get('rebate')
    write_row(summary, end + 1, ['', 'Rebate', '', '', rebate])
    after_rebate = sums[2] - rebate if rebate is not None else None
    write_row(summary, end + 2, ['', 'Total After Rebate', '', '', after_rebate])
    write_row(summary, end + 3, ['', 'VAT', '', '', values[11]])
    write_row(summary, end + 4, ['', 'Grand Total Cost', '', '', after_rebate + values[11] if after_rebate is not None else None])
    signatures(summary, end + 7, 6, [(role, '', '') for role in ('Contractor', 'Consultant', 'Employer')])
    summary.repeat_rows(5, 6)
    summary.print_area(0, 0, end + 10, 5)

    detail.merge_range('A8:L8', 'Measurement of Executed Work', title)
    for col, caption in enumerate(('Item No.', 'Description', 'Unit')):
        detail.merge_range(10, col, 11, col, caption, head)
    detail.merge_range('D11:G11', 'Quantity', head)
    detail.merge_range('H11:L11', f'Amount ({currency})', head)
    detail.write_row(11, 3, ['Contract', 'Previous', 'Current', 'To Date', 'Unit Rate', 'Contract',
                            'Previous', 'Current', 'To Date'], head)
    detail.set_row(11, 22)
    index = 12
    groups = {}
    for line in data['detail']:
        groups.setdefault(line[0], []).append(line)
    for section, lines in groups.items():
        detail.merge_range(index, 0, index, 11, section, head)
        index += 1
        for row in lines:
            detail.write_row(index, 0, row[1:4], cell)
            detail.write_row(index, 3, [row[col] for col in (5, 7, 9, 11)], quantity)
            detail.write_number(index, 7, row[4], rate)
            detail.write_row(index, 8, [row[col] for col in (6, 8, 10, 12)], number)
            detail.set_row(index, _description_height(row[2] or '', 53))
            index += 1
        write_row(detail, index, ['', 'Section Subtotal', '', '', '', '', '', '',
                                  *[sum(row[col] for row in lines) for col in (6, 8, 10, 12)]])
        index += 1
    detail.merge_range(index, 0, index, 7, 'Grand Total (Untaxed)', head)
    detail.write_row(index, 8, [sum(row[col] for row in data['detail']) for col in (6, 8, 10, 12)], number)
    detail.repeat_rows(10, 11)
    detail.freeze_panes(12, 3)
    detail.print_area(0, 0, index, 11)
