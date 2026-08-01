"""Offline smoketests for Lidl receipt parsing (no network)."""

from __future__ import annotations

from decimal import Decimal

from addons.lidl.parse import parse_html_printed_receipt, parse_items_line, parse_receipt_items


def test_items_line():
    items = parse_items_line(
        [
            {
                "name": "Wholemeal Bread 800g",
                "codeInput": "0000000054321",
                "quantity": "1",
                "currentUnitPrice": "0.55",
                "originalAmount": "0.55",
                "isWeight": False,
                "taxGroupName": "A",
                "discounts": [],
            },
            {
                "name": "Apples Pink Lady 1kg",
                "codeInput": "0000000099887",
                "quantity": "1.20",
                "currentUnitPrice": "1.99",
                "originalAmount": "2.39",
                "isWeight": True,
                "taxGroupName": "A",
                "discounts": [{"description": "Offer", "amount": "0.30"}],
            },
        ]
    )
    assert len(items) == 2
    assert items[0].product_id == "0000000054321"
    assert items[0].net_total == Decimal("0.55")
    assert items[1].is_weight is True
    assert items[1].discount_total == Decimal("0.30")
    assert items[1].net_total == Decimal("2.09")


def test_html_printed_receipt():
    html = """
    <span id="purchase_list_line_2" class="article" data-art-id="0082904"
          data-unit-price="1.08" data-tax-type="A"
          data-art-description="Broccoli 0082904">Broccoli 0082904                    1.08 A</span>
    <span id="purchase_list_line_8" class="article" data-art-id="5235083"
          data-art-quantity="2" data-unit-price="1.45" data-tax-type="A"
          data-art-description="H Protein Greek Style Yogurt">H Protein Greek Styl2 x &pound;1.45       2.90 A</span>
    <span class="css_bold">Price Cut</span>
    <span class="css_bold">-0.04</span>
    <span class="article" data-art-id="0080000" data-art-quantity="1.02"
          data-unit-price="0.90" data-tax-type="A"
          data-art-description="Bananas Loose 0080000">Bananas Loose 0080000               0.92 A</span>
    <span class="article" data-art-id="0080000" data-art-quantity="1.02"
          data-unit-price="0.90" data-tax-type="A"
          data-art-description="Bananas Loose 0080000">  1.020 kg @ &pound;0.90/kg       </span>
    """
    items = parse_html_printed_receipt(html)
    assert len(items) == 3
    assert items[0].description == "Broccoli"
    assert items[0].unit_price == Decimal("1.08")
    assert items[1].quantity == Decimal("2")
    assert items[1].line_total == Decimal("2.90")
    assert items[1].discount_total == Decimal("0.04")
    assert items[1].net_total == Decimal("2.86")
    assert items[2].is_weight is True
    assert items[2].line_total == Decimal("0.92")


def test_total_footer_not_applied_as_discount():
    """GB receipts put TOTAL 40.60 in css_bold after the last article — not a line discount."""
    html = """
    <span class="purchase_list">
    <span class="article" data-art-id="0080838" data-unit-price="2.69"
          data-tax-type="A" data-art-description="Raspberries 250g">Raspberries 250g  2.69 A</span>
    <span class="css_bold">Price Cut</span>
    <span class="css_bold">-0.04</span>
    <span class="article" data-art-id="5215510" data-unit-price="1.49"
          data-tax-type="A" data-art-description="Seeded Craft Bloomer">Seeded Craft Bloomer  1.49 A</span>
    </span>
    <span class="purchase_summary">
    <span class="css_bold">TOTAL</span>
    <span class="css_bold">40.60</span>
    </span>
    <span class="purchase_tender_information">
    <span class="css_bold">TOTAL DISCOUNT</span>
    <span class="css_bold">0.04</span>
    </span>
    """
    items = parse_html_printed_receipt(html)
    assert len(items) == 2
    assert items[0].net_total == Decimal("2.65")
    assert items[1].description == "Seeded Craft Bloomer"
    assert items[1].discount_total == Decimal("0.00")
    assert items[1].net_total == Decimal("1.49")


def test_detect_format_preference():
    payload = {
        "itemsLine": [
            {
                "name": "Milk",
                "codeInput": "1",
                "quantity": "1",
                "currentUnitPrice": "1.00",
                "originalAmount": "1.00",
            }
        ],
        "htmlPrintedReceipt": "<span class='article'></span>",
    }
    items = parse_receipt_items(payload)
    assert len(items) == 1
    assert items[0].description == "Milk"


if __name__ == "__main__":
    test_items_line()
    test_html_printed_receipt()
    test_detect_format_preference()
    print("parse smoketests ok")
