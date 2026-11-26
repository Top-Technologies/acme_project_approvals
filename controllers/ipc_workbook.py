from .boq_workbook import _description_height


from .boq_workbook import _description_height


def write_client_workbook(book, data):
    cover = data.get('cover', [])
    values = [row[1] for row in cover] if cover else []
    round_name = values[0] if len(values) > 0 else 'Round 1'
    project = values[1] if len(values) > 1 else ''
    reference = values[2] if len(values) > 2 else ''
    code = values[3] if len(values) > 3 else ''
    client = values[4] if len(values) > 4 else ''
    site = values[5] if len(values) > 5 else ''
    period = values[6] if len(values) > 6 else ''
    currency = values[7] if len(values) > 7 else 'USD'
    meta = data.get('client_excel', {})
    ercs = data.get('ercs_ipc', {})

    # Formats
    quantity = book.add_format({'border': 1, 'num_format': '#,##0.00;[Red](#,##0.00);0.00', 'align': 'right', 'valign': 'vcenter'})
    rate = book.add_format({'border': 1, 'num_format': '#,##0.00######', 'align': 'right', 'valign': 'vcenter'})
    number = book.add_format({'border': 1, 'num_format': '#,##0.00;[Red](#,##0.00);0.00', 'align': 'right', 'valign': 'vcenter'})
    bold_number = book.add_format({'border': 1, 'bold': True, 'num_format': '#,##0.00;[Red](#,##0.00);0.00', 'align': 'right', 'valign': 'vcenter', 'bg_color': '#f2f5f3'})
    cell = book.add_format({'border': 1, 'text_wrap': True, 'valign': 'top'})
    head = book.add_format({'border': 1, 'bold': True, 'align': 'center', 'valign': 'vcenter',
                            'text_wrap': True, 'bg_color': '#e7edf3'})
    title = book.add_format({'bold': True, 'font_size': 14, 'align': 'center', 'valign': 'vcenter'})
    subtitle = book.add_format({'bold': True, 'font_size': 11, 'align': 'center', 'valign': 'vcenter'})
    label = book.add_format({'bold': True, 'text_wrap': True, 'valign': 'vcenter'})
    wrapped = book.add_format({'text_wrap': True, 'valign': 'vcenter'})
    section_head = book.add_format({'border': 1, 'bold': True, 'bg_color': '#f2f5f3', 'valign': 'vcenter'})

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

    def write_row(ws, row, cells, is_bold=False):
        fmt = bold_number if is_bold else number
        for col, val in enumerate(cells):
            if isinstance(val, (float, int)):
                ws.write(row, col, val, fmt)
            else:
                ws.write(row, col, val, section_head if is_bold else cell)

    def signatures(ws, row, width, roles):
        for index, (role, person, signed_on) in enumerate(roles):
            first = index * width // len(roles)
            last = (index + 1) * width // len(roles) - 1
            for offset, text in enumerate((role, 'Name: ' + (person or '________________'),
                                          'Signature: __________________', 'Date: ' + (signed_on or '____________'))):
                ws.merge_range(row + offset, first, row + offset, last, text, label if not offset else wrapped)

    # ==========================================
    # 1. WORKSHEET: IPC (ERCS Payment Certificate)
    # ==========================================
    ipc = sheet('IPC', (14.0, 18.0, 14.0, 14.0, 14.0, 24.0, 20.0, 18.0), True)
    round_title = ercs.get('round_title', f'Payment Certificate - {round_name.split()[-1]}')
    round_subtitle = ercs.get('round_subtitle', '(Interim Payment)')
    ipc.merge_range('A2:H2', round_title, title)
    ipc.merge_range('A3:H3', round_subtitle, subtitle)
    ipc.set_row(1, 24)
    ipc.set_row(2, 18)

    meta_rows = [
        ('Project Name:', ercs.get('project_name', project)),
        ('Project Location:', ercs.get('location', site)),
        ('Consultant:', ercs.get('consultant', 'ERCS')),
        ('Client:', ercs.get('client', client)),
        ('Contractor:', ercs.get('contractor', meta.get('contractor', ''))),
        ('Date:', str(ercs.get('cert_date', ''))),
    ]
    for row_idx, (cap, val) in enumerate(meta_rows, 4):
        ipc.write(row_idx, 0, cap, label)
        ipc.merge_range(row_idx, 1, row_idx, 7, str(val), wrapped)
        ipc.set_row(row_idx, 18)

    # Contract Value Box (Right / Upper)
    ipc.merge_range('E11:G11', 'Contract Value', head)
    ipc.write('H11', f'Amount ({currency})', head)
    contract_items = [
        ('Main-Service sum', ercs.get('main_service_sum', meta.get('contract', 0.0))),
        ('Supplementary contract', ercs.get('supplementary', 0.0)),
        ('Variation Order No.1', ercs.get('variations', 0.0)),
        ('Sub-Total', ercs.get('subtotal', meta.get('contract', 0.0))),
        ('VAT ( 15%)', ercs.get('vat_contract', 0.0)),
        ('Total', ercs.get('total_contract', 0.0)),
    ]
    for idx, (cap, val) in enumerate(contract_items, 11):
        ipc.merge_range(idx, 4, idx, 6, cap, section_head if 'Total' in cap else cell)
        ipc.write(idx, 7, val or 0.0, bold_number if 'Total' in cap else number)
        ipc.set_row(idx, 18)

    # Supervisor statement
    sup_text = f"According to the measurement taken by supervisor (s) of the works {ercs.get('supervisor', 'Supervisor')}\nthe measurement and value of the work executed and/or materials supplied up to date is"
    ipc.merge_range('A18:H19', sup_text, wrapped)

    # Executed Work & Deductions Table
    # Left: History (Purpose, Service sum, VAT)
    ipc.write('A21', 'Purpose', head)
    ipc.merge_range('B21:C21', f'Service sum ({currency})', head)
    ipc.write('D21', f'VAT ({currency})', head)
    adv_total = ercs.get('advance_total', 0.0)
    adv_rec = ercs.get('ded_3_advance_recovery', 0.0)
    prev_work = ercs.get('ded_1_prev_payments', 0.0)
    curr_work = ercs.get('gross_work', 0.0) - prev_work
    ipc_history = [
        ('Advance', adv_total, adv_total * 0.15),
        ('Adv. Repay', adv_rec, adv_rec * 0.15),
        ('Previous Claims', prev_work, prev_work * 0.15),
        ('This Claim', curr_work, curr_work * 0.15),
    ]
    for idx, (purp, s_sum, v_amt) in enumerate(ipc_history, 21):
        ipc.write(idx, 0, purp, cell)
        ipc.merge_range(idx, 1, idx, 2, s_sum, number)
        ipc.write(idx, 3, v_amt, number)
        ipc.set_row(idx, 18)
    ipc.write(25, 0, 'Total Previous', section_head)
    ipc.merge_range(25, 1, 25, 2, prev_work, bold_number)
    ipc.write(25, 3, prev_work * 0.15, bold_number)

    # Right: Deductions (A through J)
    ipc.merge_range('E21:G21', 'Executed & Deductions', head)
    ipc.write('H21', f'Amount ({currency})', head)
    ret_pct = ercs.get('retention_percent', 5.0)
    ded_rows = [
        ('A = Total Executed Service Sum', ercs.get('gross_work', 0.0), True),
        ('1. Previous payments (only Service sum)', ercs.get('ded_1_prev_payments', 0.0), False),
        (f'2. Retention = (A - Pre. Service sum) * {ret_pct}%', ercs.get('ded_2_retention', 0.0), False),
        ('3. Advance Rep. = (Total Advance - Prev. Repaid)', ercs.get('ded_3_advance_recovery', 0.0), False),
        ('4. Penalty / Withholding', ercs.get('ded_4_withholding', 0.0), False),
        ('B = Total deduction', ercs.get('total_deduction', 0.0), True),
        ('C = Net (A - B)', ercs.get('net_before_vat', 0.0), True),
        ('D = VAT (15% of A)', ercs.get('vat_gross', 0.0), False),
        ('E = Previously paid VAT', ercs.get('prev_vat', 0.0), False),
        ('F = VAT Advance Rep.', ercs.get('vat_recovery', 0.0), False),
        ('G = VAT of Retention', 0.0, False),
        ('H = This VAT (D - E - F - G)', ercs.get('this_vat', 0.0), True),
        ('I = Retention refund', ercs.get('retention_refund', 0.0), False),
        ('J = Net pay to contractor (C + H)', ercs.get('net_payable', 0.0), True),
    ]
    for idx, (lbl, val, is_b) in enumerate(ded_rows, 21):
        ipc.merge_range(idx, 4, idx, 6, lbl, section_head if is_b else cell)
        ipc.write(idx, 7, val, bold_number if is_b else number)
        ipc.set_row(idx, 18)

    # Bottom certification
    net_val = ercs.get('net_payable', 0.0)
    cert_txt = f"We/I certify the contractor is entitled to the sum of {currency} {net_val:,.2f}\n({ercs.get('amount_words', meta.get('amount_words', ''))} only)."
    ipc.merge_range('A36:H37', cert_txt, wrapped)

    # Signatures
    signatures(ipc, 39, 8, [
        ('Prepared by: PM', ercs.get('prepared_by', ''), ercs.get('prepared_date', '')),
        ('Checked by: DM', ercs.get('checked_by', ''), ercs.get('checked_date', '')),
        ('Approved by: Certified', ercs.get('approved_by', ''), ercs.get('approved_date', '')),
    ])
    ipc.print_area(0, 0, 44, 7)

    # ==========================================
    # 2. WORKSHEET: Summary (VSF Grand Summary)
    # ==========================================
    summary = sheet('Summary', (8.0, 48.0, 18.0, 18.0, 18.0, 18.0))
    summary_meta = [
        ('Project Name:', ercs.get('project_name', project)),
        ('Employer:', ercs.get('client', client)),
        ('Subcontractor:', ercs.get('contractor', meta.get('contractor', ''))),
        ('Location:', ercs.get('location', site)),
    ]
    for r_idx, (cap, val) in enumerate(summary_meta):
        summary.write(r_idx, 0, cap, label)
        summary.merge_range(r_idx, 1, r_idx, 5, str(val), wrapped)
        summary.set_row(r_idx, 20)

    summary.merge_range('A6:F6', 'Grand Summary', title)
    summary.set_row(5, 26)
    summary.write_row(6, 0, [
        'No.', 'Description', f'Contract Amount ({currency})',
        f'Previous Executed Amount ({currency})', f'Current Executed Amount ({currency})',
        f'To-Date Executed Amount ({currency})',
    ], head)
    summary.set_row(6, 32)

    for index, row in enumerate(data.get('summary', []), 1):
        write_row(summary, 6 + index, [index, row[0], row[1], row[2], row[3], row[4]])
        summary.set_row(6 + index, 24)

    s_end = 7 + len(data.get('summary', []))
    s_sums = [sum(row[col] for row in data.get('summary', [])) for col in (1, 2, 3, 4)]
    write_row(summary, s_end, ['', 'Total', *s_sums], is_bold=True)
    summary.set_row(s_end, 22)

    rebate_amt = ercs.get('rebate', meta.get('rebate', 0.0)) or 0.0
    write_row(summary, s_end + 1, ['', 'Rebate (_%)', 0.0, 0.0, rebate_amt, rebate_amt])
    summary.set_row(s_end + 1, 20)

    after_rebate = [s_sums[0], s_sums[1], s_sums[2] - rebate_amt, s_sums[3] - rebate_amt]
    write_row(summary, s_end + 2, ['', 'Total After Rebate', *after_rebate], is_bold=True)
    summary.set_row(s_end + 2, 22)

    vat_sums = [round(v * 0.15, 2) for v in after_rebate]
    write_row(summary, s_end + 3, ['', 'VAT (15%)', *vat_sums])
    summary.set_row(s_end + 3, 20)

    grand_total = [after_rebate[i] + vat_sums[i] for i in range(4)]
    write_row(summary, s_end + 4, ['', 'Grand Total Cost', *grand_total], is_bold=True)
    summary.set_row(s_end + 4, 24)

    signatures(summary, s_end + 6, 6, [
        ('Contractor', ercs.get('contractor', meta.get('contractor', '')), str(ercs.get('cert_date', ''))),
        ('Consultant', ercs.get('consultant', 'ERCS'), str(ercs.get('cert_date', ''))),
        ('Employer', ercs.get('client', client), ''),
    ])
    summary.repeat_rows(5, 6)
    summary.print_area(0, 0, s_end + 10, 5)

    # ==========================================
    # 3. WORKSHEET: BOQ (ERCS Genete Mariam Format)
    # ==========================================
    boq = sheet('BOQ', (8.0, 52.0, 8.0, 11.0, 11.0, 11.0, 11.0, 12.0, 14.0, 14.0, 14.0, 14.0))
    for r_idx, (cap, val) in enumerate(summary_meta):
        boq.write(r_idx, 0, cap, label)
        boq.merge_range(r_idx, 1, r_idx, 11, str(val), wrapped)
        boq.set_row(r_idx, 20)

    boq.merge_range('A6:L6', f'Measurement of Executed Work ({round_name})', title)
    boq.set_row(5, 26)
    sub_bill_name = ercs.get('project_name', project)
    boq.merge_range('A7:L7', f'1) Bill of quantities and specification for {sub_bill_name}', subtitle)
    boq.set_row(6, 22)

    # Two-tier header
    for c_idx, cap in enumerate(('Item No.', 'Description', 'Unit')):
        boq.merge_range(7, c_idx, 8, c_idx, cap, head)
    boq.merge_range('D8:G8', 'Quantity', head)
    boq.merge_range(7, 7, 8, 7, f'Unit Rate\n({currency})', head)
    boq.merge_range('I8:L8', f'Total Amount ({currency})', head)
    boq.write_row(8, 3, ['Contract', 'Previous', 'Current', 'To Date'], head)
    boq.write_row(8, 8, ['Contract', 'Previous', 'Current', 'To Date'], head)
    boq.set_row(7, 20)
    boq.set_row(8, 22)

    b_idx = 9
    groups = {}
    for line in data.get('detail', []):
        groups.setdefault(line[0], []).append(line)

    for section, lines in groups.items():
        boq.merge_range(b_idx, 0, b_idx, 11, section, section_head)
        boq.set_row(b_idx, 22)
        b_idx += 1
        for row in lines:
            boq.write_row(b_idx, 0, row[1:4], cell)
            boq.write_row(b_idx, 3, [row[col] for col in (5, 7, 9, 11)], quantity)
            boq.write_number(b_idx, 7, row[4], rate)
            boq.write_row(b_idx, 8, [row[col] for col in (6, 8, 10, 12)], number)
            boq.set_row(b_idx, _description_height(row[2] or '', 50))
            b_idx += 1
        # Section Subtotal
        sec_sums = [sum(row[col] for row in lines) for col in (6, 8, 10, 12)]
        boq.merge_range(b_idx, 0, b_idx, 7, f'Total Cost {section}', section_head)
        boq.write_row(b_idx, 8, sec_sums, bold_number)
        boq.set_row(b_idx, 22)
        b_idx += 1

    # Grand Total
    total_contract = sum(row[6] for row in data.get('detail', []))
    total_prev = sum(row[8] for row in data.get('detail', []))
    total_curr = sum(row[10] for row in data.get('detail', []))
    total_todate = sum(row[12] for row in data.get('detail', []))
    boq.merge_range(b_idx, 0, b_idx, 7, 'Total Cost material, transportation, equipment and labor', head)
    boq.write_row(b_idx, 8, [total_contract, total_prev, total_curr, total_todate], bold_number)
    boq.set_row(b_idx, 26)

    boq.repeat_rows(7, 8)
    boq.freeze_panes(9, 3)
    boq.print_area(0, 0, b_idx, 11)
