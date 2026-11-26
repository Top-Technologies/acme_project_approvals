from collections import defaultdict
from textwrap import wrap


def _description_height(description, width):
    lines = sum(max(1, len(wrap(text, width))) for text in str(description or '').split('\n'))
    return min(400, max(22, 16 * lines + 6))


def write_project_boq(book, project, lines):
    """Generates the Measurement of Executed Work / Bill of Quantities export workbook

    matching the client engineering format:
    - Header: Project Name, Location, Consultant, Client, Contractor
    - Title: Measurement of Executed Work
    - Subtitle / Lot: Bill of quantities and Specification...
    - Columns:
      Item No | Description | Unit | Quantity (Contract, Executed) | Unit Price | Total Amount (Contract, Executed)
    """
    currency = project.acme_contract_currency_id
    curr_name = currency.name if currency else 'ETB'
    round_amount = currency.round if currency else lambda amount: round(amount, 2)

    ws = book.add_worksheet('Measurement of Executed Work')
    ws.set_paper(9)  # A4
    ws.fit_to_pages(1, 0)
    ws.set_margins(0.3, 0.3, 0.4, 0.4)
    ws.hide_gridlines(2)
    ws.set_footer('&RPage &P of &N')

    # Column widths: Item No, Description, Unit, Qty Contract, Qty Executed, Unit Price, Total Contract, Total Executed
    widths = [10, 52, 8, 12, 12, 14, 18, 18]
    for col, width in enumerate(widths):
        ws.set_column(col, col, width)

    # Styles
    base_font = {'font_name': 'Calibri', 'font_size': 10, 'valign': 'vcenter'}
    meta_label_style = dict(base_font, bold=True, font_size=11)
    meta_value_style = dict(base_font, font_size=11)

    border_all = dict(base_font, border=1)

    # Headers
    title_format = book.add_format(dict(base_font, bold=True, font_size=14, align='center', valign='vcenter', underline=1))
    lot_title_format = book.add_format(dict(base_font, bold=True, font_size=11, align='left', valign='vcenter'))

    tbl_header = book.add_format(dict(border_all, bold=True, bg_color='#D9E1F2', align='center', valign='vcenter', text_wrap=True))
    sec_header_1 = book.add_format(dict(border_all, bold=True, bg_color='#BDD7EE', align='left', valign='vcenter'))
    sec_header_2 = book.add_format(dict(border_all, bold=True, bg_color='#F2F2F2', align='left', valign='vcenter'))

    text_center = book.add_format(dict(border_all, align='center'))
    text_left = book.add_format(dict(border_all, align='left', text_wrap=True))

    qty_fmt = book.add_format(dict(border_all, align='right', num_format='#,##0.00'))
    qty_int_fmt = book.add_format(dict(border_all, align='right', num_format='#,##0'))

    dec = currency.decimal_places if currency else 2
    rate_fmt = book.add_format(dict(border_all, align='right', num_format='#,##0.' + '0' * dec))
    amt_fmt = book.add_format(dict(border_all, align='right', num_format='#,##0.' + '0' * dec))

    total_label_fmt = book.add_format(dict(border_all, bold=True, bg_color='#D9E1F2', align='left'))
    total_val_fmt = book.add_format(dict(border_all, bold=True, bg_color='#D9E1F2', align='right', num_format='#,##0.' + '0' * dec))

    # 1. Metadata Header (Rows 1 to 6)
    partner = project.partner_id
    client_name = partner.display_name if partner else ''
    contractor_name = project.company_id.name or 'Acme Engineering and Trading Plc'

    metadata = [
        ('Project Name:', project.display_name or ''),
        ('Project Location:', getattr(project, 'acme_site', '') or getattr(project, 'acme_project_sequence', '') or ''),
        ('Consultant:', getattr(project, 'acme_consultant', '') or 'ERCS'),
        ('Client:', client_name),
        ('Contractor:', contractor_name),
    ]

    for r_idx, (lbl, val) in enumerate(metadata):
        ws.write(r_idx, 0, lbl, book.add_format(meta_label_style))
        ws.merge_range(r_idx, 1, r_idx, 7, val, book.add_format(meta_value_style))
        ws.set_row(r_idx, 20)

    # 2. Sheet Title & Lot Title (Rows 7 to 9)
    ws.merge_range('A8:H8', 'Measurement of Executed Work', title_format)
    ws.set_row(7, 26)

    lot_name = project.acme_contract_reference or ('BOQ for ' + (project.name or 'Project'))
    ws.merge_range('A10:H10', f'Bill of quantities and Specification for {lot_name}', lot_title_format)
    ws.set_row(9, 22)

    # 3. Table Column Headers (Rows 11 and 12, 0-indexed: 10 and 11)
    # Row 10 (top header)
    ws.merge_range(11, 0, 12, 0, 'Item No', tbl_header)
    ws.merge_range(11, 1, 12, 1, 'Description', tbl_header)
    ws.merge_range(11, 2, 12, 2, 'Unit', tbl_header)
    ws.merge_range(11, 3, 11, 4, 'Quantity', tbl_header)
    ws.merge_range(11, 5, 12, 5, f'Unit Price\n({curr_name})', tbl_header)
    ws.merge_range(11, 6, 11, 7, f'Total Amount\n({curr_name})', tbl_header)

    # Row 11 (sub header for Quantity & Total Amount)
    ws.write(12, 3, 'Contract', tbl_header)
    ws.write(12, 4, 'Executed', tbl_header)
    ws.write(12, 6, 'Contract', tbl_header)
    ws.write(12, 7, 'Executed', tbl_header)

    ws.set_row(11, 24)
    ws.set_row(12, 24)
    ws.repeat_rows(11, 12)
    ws.freeze_panes(13, 0)

    # Group lines by Bill No and Category
    sections = defaultdict(list)
    for line in lines:
        bill = (line.bill_no or '').strip()
        cat = (line.category or '').strip()
        sections[(bill, cat)].append(line)

    current_row = 13
    total_contract_amt = 0.0
    total_executed_amt = 0.0

    last_bill = None

    for (bill, cat), section_lines in sections.items():
        # Level 1 Section Header (e.g., "A Civil Works" or "B Solar and Pump Work")
        if bill and bill != last_bill:
            ws.write(current_row, 0, bill, sec_header_1)
            ws.merge_range(current_row, 1, current_row, 7, bill, sec_header_1)
            ws.set_row(current_row, 22)
            current_row += 1
            last_bill = bill

        # Level 2 Section Header (e.g., "1 Earth Work" or Category)
        if cat:
            ws.write(current_row, 0, '', sec_header_2)
            ws.merge_range(current_row, 1, current_row, 7, cat, sec_header_2)
            ws.set_row(current_row, 20)
            current_row += 1

        # Section Item Lines
        for idx, line in enumerate(section_lines, 1):
            item_ref = line.reference or (f'{bill}.{idx}' if bill else str(idx))
            desc = line.name or line.product_name or ''
            uom = line.unit or ''

            c_qty = line.quantity or line.acme_original_quantity or 0.0
            e_qty = line.acme_verified_executed_qty or line.executed_quantity or 0.0

            price = line.selling_rate or line.unit_rate or 0.0
            c_amt = round_amount(c_qty * price)
            e_amt = line.acme_verified_executed_amount or round_amount(e_qty * price)

            total_contract_amt += c_amt
            total_executed_amt += e_amt

            q_fmt_c = qty_int_fmt if c_qty == int(c_qty) else qty_fmt
            q_fmt_e = qty_int_fmt if e_qty == int(e_qty) else qty_fmt

            ws.write(current_row, 0, item_ref, text_center)
            ws.write(current_row, 1, desc, text_left)
            ws.write(current_row, 2, uom, text_center)
            ws.write_number(current_row, 3, c_qty, q_fmt_c)
            ws.write_number(current_row, 4, e_qty, q_fmt_e)
            ws.write_number(current_row, 5, price, rate_fmt)
            ws.write_number(current_row, 6, c_amt, amt_fmt)
            ws.write_number(current_row, 7, e_amt, amt_fmt)

            ws.set_row(current_row, _description_height(desc, 50))
            current_row += 1

    # 4. Total Row at Bottom
    ws.merge_range(current_row, 0, current_row, 5, f'Total Cost Material, Transportation, equipment and labor ({curr_name})', total_label_fmt)
    ws.write_number(current_row, 6, round_amount(total_contract_amt), total_val_fmt)
    ws.write_number(current_row, 7, round_amount(total_executed_amt), total_val_fmt)
    ws.set_row(current_row, 26)

    ws.print_area(0, 0, current_row, 7)
