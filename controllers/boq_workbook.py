from collections import defaultdict
from textwrap import wrap


def _description_height(description, width):
    lines = sum(max(1, len(wrap(text, width))) for text in description.split('\n'))
    return min(400, max(20, 15 * lines + 5))


def write_project_boq(book, project, lines):
    _ = project.env._
    currency = project.acme_contract_currency_id
    round_amount = currency.round if currency else lambda amount: amount
    originals = lines._acme_signed_boq_values(project)
    ws = book.add_worksheet('Project BOQ')
    ws.set_paper(9)
    ws.set_landscape()
    ws.fit_to_pages(1, 0)
    ws.set_margins(.25, .25, .35, .35)
    ws.hide_gridlines(2)
    ws.set_footer('&RPage &P of &N')
    widths = (8, 50, 9, 10, 10, 9, 12, 12, 14, 14, 12, 12, 14, 14, 10, 14)
    for col, width in enumerate(widths):
        ws.set_column(col, col, width)

    font = {'font_name': 'Times New Roman', 'font_size': 11, 'valign': 'top'}
    border = dict(font, border=1)
    title = book.add_format(dict(font, bold=True, font_size=15, align='center'))
    metadata = book.add_format(dict(font, text_wrap=True))
    heading = book.add_format(dict(border, bold=True, bg_color='#E7EBE9', align='center',
                                   valign='vcenter', text_wrap=True))
    section_style = book.add_format(dict(border, bold=True, bg_color='#F2F5F3'))
    text = book.add_format(dict(border, text_wrap=True))
    qty = book.add_format(dict(border, num_format='#,##0.############'))
    signed = book.add_format(dict(border, num_format='+#,##0.############;"−"#,##0.############;0'))
    signed_integer = book.add_format(dict(border, num_format='+#,##0;"−"#,##0;0'))
    rate = book.add_format(dict(border, num_format='#,##0.00######'))
    amount_style = {'num_format': '#,##0.' + '0' * (currency.decimal_places if currency else 2)}
    if currency and not currency.decimal_places:
        amount_style['num_format'] = '#,##0'
    money = book.add_format(dict(border, **amount_style))
    total_label = book.add_format(dict(border, bold=True, bg_color='#F2F5F3'))
    total_money = book.add_format(dict(border, bold=True, bg_color='#F2F5F3', **amount_style))

    address = project.partner_id
    location = project.acme_site if 'acme_site' in project._fields else ''
    for row, (label, value) in enumerate((
        (_('Project Name'), project.display_name),
        (_('Project Location'), location),
        (_('Client'), address.display_name),
        (_('Contractor'), project.company_id.name),
        (_('Contract Reference'), project.acme_contract_reference),
        (_('Project Code'), project.acme_project_sequence if 'acme_project_sequence' in project._fields else ''),
        (_('Currency'), currency.name),
    )):
        ws.merge_range(row, 0, row, 2, label, metadata)
        ws.merge_range(row, 3, row, 15, value or '', metadata)
        ws.set_row(row, _description_height(value or '', 115))
    ws.merge_range(7, 0, 7, 15, _('Bill of Quantities'), title)
    ws.set_row(7, 25)

    for col, caption in enumerate((_('Item No.'), _('Description'), _('Unit'))):
        ws.merge_range(9, col, 10, col, caption, heading)
    groups = (
        (3, 5, _('Quantity'), [_('Original Qty'), _('Revised Qty'), _('Variation')]),
        (6, 9, _('Contract Selling Value (Untaxed)'), [
            _('Original Rate'), _('Revised Rate'), _('Original Amount'), _('Revised Amount')]),
        (10, 13, _('Tender / Source BOQ Value (Untaxed)'), [
            _('Original Rate'), _('Revised Rate'), _('Original Amount'), _('Revised Amount')]),
        (14, 15, _('Verified Execution'), [_('Executed Qty'), _('Executed Amount')]),
    )
    for first, last, caption, labels in groups:
        ws.merge_range(9, first, 9, last, caption, heading)
        ws.write_row(10, first, labels, heading)
    ws.set_row(9, 24)
    ws.set_row(10, 30)
    ws.repeat_rows(9, 10)
    ws.freeze_panes(11, 3)

    sections = defaultdict(list)
    for line in lines:
        sections[(line.bill_no, line.category)].append(line)
    totals = dict.fromkeys((8, 9, 12, 13, 15), 0)
    row = 11

    def write_total(row, caption, values):
        ws.merge_range(row, 0, row, 7, caption, total_label)
        for col in range(8, 16):
            ws.write(row, col, values.get(col), total_money)

    for (bill, category), section_lines in sections.items():
        caption = ' — '.join(filter(None, (bill, category))) or _('Unsectioned')
        ws.merge_range(row, 0, row, 15, caption, section_style)
        row += 1
        subtotal = dict.fromkeys(totals, 0)
        for item, line in enumerate(section_lines, 1):
            original = originals.get(line.id, {})
            original_qty = line.acme_original_quantity
            old_rate = original.get('selling_rate', line.selling_rate)
            old_source_rate = original.get('unit_rate', line.unit_rate)
            values = {
                8: round_amount(original_qty * old_rate),
                9: round_amount(line.quantity * line.selling_rate),
                12: round_amount(original_qty * old_source_rate),
                13: round_amount(line.quantity * line.unit_rate),
                15: line.acme_verified_executed_amount,
            }
            ws.write_row(row, 0, [line.reference or (f'{bill}.{item}' if bill else str(item)),
                                  line.name, line.unit or ''], text)
            ws.write_row(row, 3, [original_qty, line.quantity], qty)
            delta = line._acme_quantity_delta()
            ws.write_number(row, 5, delta, signed_integer if delta == int(delta) else signed)
            ws.write_row(row, 6, [old_rate, line.selling_rate], rate)
            ws.write_row(row, 10, [old_source_rate, line.unit_rate], rate)
            ws.write_number(row, 14, line.acme_verified_executed_qty, qty)
            for col, value in values.items():
                ws.write_number(row, col, value, money)
                subtotal[col] += value
                totals[col] += value
            ws.set_row(row, _description_height(line.name, 48))
            row += 1
        write_total(row, _('Section Subtotal'), subtotal)
        row += 1
    write_total(row, _('Grand Total (Untaxed)'), totals)
    ws.print_area(0, 0, row, 15)