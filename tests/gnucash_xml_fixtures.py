"""A GnuCash XML book carrying scheduled transactions.

Reproduces the structure that caused template accounts to appear in the ledger:
GnuCash stores scheduled-transaction templates in a ``<gnc:template-transactions>``
section using the *same* element names as the real book, and names each template
account after the GUID of the schedule it belongs to. An importer that walks
elements by name alone therefore fills the register with transactions posted to
long hexadecimal "accounts", and finds no schedules at all.
"""

from __future__ import annotations

import gzip
from pathlib import Path
from types import SimpleNamespace

from gnucash_fixtures import new_guid

__all__ = ["create_xml_book", "XML_WITH_SCHEDULE"]

XML_WITH_SCHEDULE = """<?xml version="1.0" encoding="utf-8" ?>
<gnc-v2
     xmlns:gnc="http://www.gnucash.org/XML/gnc"
     xmlns:act="http://www.gnucash.org/XML/act"
     xmlns:trn="http://www.gnucash.org/XML/trn"
     xmlns:split="http://www.gnucash.org/XML/split"
     xmlns:cmdty="http://www.gnucash.org/XML/cmdty"
     xmlns:ts="http://www.gnucash.org/XML/ts"
     xmlns:cd="http://www.gnucash.org/XML/cd"
     xmlns:slot="http://www.gnucash.org/XML/slot"
     xmlns:sx="http://www.gnucash.org/XML/sx"
     xmlns:recurrence="http://www.gnucash.org/XML/recurrence">
<gnc:count-data cd:type="book">1</gnc:count-data>
<gnc:book version="2.0.0">
  <gnc:commodity version="2.0.0">
    <cmdty:space>CURRENCY</cmdty:space>
    <cmdty:id>USD</cmdty:id>
    <cmdty:name>US Dollar</cmdty:name>
    <cmdty:fraction>100</cmdty:fraction>
  </gnc:commodity>

  <gnc:account version="2.0.0">
    <act:name>Root Account</act:name>
    <act:id type="guid">{root}</act:id>
    <act:type>ROOT</act:type>
  </gnc:account>
  <gnc:account version="2.0.0">
    <act:name>Assets</act:name>
    <act:id type="guid">{assets}</act:id>
    <act:type>ASSET</act:type>
    <act:parent type="guid">{root}</act:parent>
    <act:slots>
      <slot><slot:key>placeholder</slot:key><slot:value type="string">true</slot:value></slot>
    </act:slots>
  </gnc:account>
  <gnc:account version="2.0.0">
    <act:name>Checking</act:name>
    <act:id type="guid">{bank}</act:id>
    <act:type>BANK</act:type>
    <act:commodity-scu>1000</act:commodity-scu>
    <act:parent type="guid">{assets}</act:parent>
  </gnc:account>
  <gnc:account version="2.0.0">
    <act:name>Expenses</act:name>
    <act:id type="guid">{expenses}</act:id>
    <act:type>EXPENSE</act:type>
    <act:parent type="guid">{root}</act:parent>
  </gnc:account>
  <gnc:account version="2.0.0">
    <act:name>Rent</act:name>
    <act:id type="guid">{rent}</act:id>
    <act:type>EXPENSE</act:type>
    <act:parent type="guid">{expenses}</act:parent>
  </gnc:account>

  <gnc:transaction version="2.0.0">
    <trn:id type="guid">{txn}</trn:id>
    <trn:currency><cmdty:space>CURRENCY</cmdty:space><cmdty:id>USD</cmdty:id></trn:currency>
    <trn:date-posted><ts:date>2026-01-02 10:59:00 +0000</ts:date></trn:date-posted>
    <trn:description>January rent</trn:description>
    <trn:splits>
      <trn:split>
        <split:id type="guid">{split1}</split:id>
        <split:reconciled-state>n</split:reconciled-state>
        <split:value>180000/100</split:value>
        <split:quantity>180000/100</split:quantity>
        <split:account type="guid">{rent}</split:account>
      </trn:split>
      <trn:split>
        <split:id type="guid">{split2}</split:id>
        <split:reconciled-state>n</split:reconciled-state>
        <split:value>-180000/100</split:value>
        <split:quantity>-180000/100</split:quantity>
        <split:account type="guid">{bank}</split:account>
      </trn:split>
    </trn:splits>
  </gnc:transaction>

  <gnc:template-transactions>
    <gnc:account version="2.0.0">
      <act:name>Template Root</act:name>
      <act:id type="guid">{template_root}</act:id>
      <act:type>ROOT</act:type>
    </gnc:account>
    <gnc:account version="2.0.0">
      <act:name>{schedule}</act:name>
      <act:id type="guid">{template_account}</act:id>
      <act:type>BANK</act:type>
      <act:parent type="guid">{template_root}</act:parent>
    </gnc:account>
    <gnc:transaction version="2.0.0">
      <trn:id type="guid">{template_txn}</trn:id>
      <trn:currency><cmdty:space>CURRENCY</cmdty:space><cmdty:id>USD</cmdty:id></trn:currency>
      <trn:date-posted><ts:date>2026-01-01 00:00:00 +0000</ts:date></trn:date-posted>
      <trn:description>Rent</trn:description>
      <trn:splits>
        <trn:split>
          <split:id type="guid">{tsplit1}</split:id>
          <split:reconciled-state>n</split:reconciled-state>
          <split:value>0/100</split:value>
          <split:quantity>0/100</split:quantity>
          <split:account type="guid">{template_account}</split:account>
          <split:slots>
            <slot>
              <slot:key>sched-xaction</slot:key>
              <slot:value type="frame">
                <slot><slot:key>account</slot:key>
                      <slot:value type="guid">{rent}</slot:value></slot>
                <slot><slot:key>debit-formula</slot:key>
                      <slot:value type="string">1800.00</slot:value></slot>
                <slot><slot:key>credit-formula</slot:key>
                      <slot:value type="string"></slot:value></slot>
              </slot:value>
            </slot>
          </split:slots>
        </trn:split>
        <trn:split>
          <split:id type="guid">{tsplit2}</split:id>
          <split:reconciled-state>n</split:reconciled-state>
          <split:value>0/100</split:value>
          <split:quantity>0/100</split:quantity>
          <split:account type="guid">{template_account}</split:account>
          <split:slots>
            <slot>
              <slot:key>sched-xaction</slot:key>
              <slot:value type="frame">
                <slot><slot:key>account</slot:key>
                      <slot:value type="guid">{bank}</slot:value></slot>
                <slot><slot:key>credit-formula</slot:key>
                      <slot:value type="string">1800.00</slot:value></slot>
                <slot><slot:key>debit-formula</slot:key>
                      <slot:value type="string"></slot:value></slot>
              </slot:value>
            </slot>
          </split:slots>
        </trn:split>
      </trn:splits>
    </gnc:transaction>
  </gnc:template-transactions>

  <gnc:schedxaction version="2.0.0">
    <sx:id type="guid">{schedule}</sx:id>
    <sx:name>Monthly rent</sx:name>
    <sx:enabled>y</sx:enabled>
    <sx:autoCreate>y</sx:autoCreate>
    <sx:autoCreateNotify>n</sx:autoCreateNotify>
    <sx:advanceCreateDays>5</sx:advanceCreateDays>
    <sx:advanceRemindDays>0</sx:advanceRemindDays>
    <sx:instanceCount>3</sx:instanceCount>
    <sx:start><gdate>2026-01-01</gdate></sx:start>
    <sx:templ-acct type="guid">{template_account}</sx:templ-acct>
    <gnc:recurrence version="1.0.0">
      <recurrence:mult>1</recurrence:mult>
      <recurrence:period_type>month</recurrence:period_type>
      <recurrence:start><gdate>2026-01-01</gdate></recurrence:start>
    </gnc:recurrence>
  </gnc:schedxaction>
</gnc:book>
</gnc-v2>
"""


def create_xml_book(path: str | Path, compress: bool = True) -> SimpleNamespace:
    """Write the book and return a namespace of the GUIDs used inside it."""
    ids = SimpleNamespace(
        root=new_guid(),
        assets=new_guid(),
        bank=new_guid(),
        expenses=new_guid(),
        rent=new_guid(),
        txn=new_guid(),
        split1=new_guid(),
        split2=new_guid(),
        template_root=new_guid(),
        template_account=new_guid(),
        template_txn=new_guid(),
        tsplit1=new_guid(),
        tsplit2=new_guid(),
        schedule=new_guid(),
    )
    body = XML_WITH_SCHEDULE.format(**vars(ids))
    path = Path(path)
    if compress:
        with gzip.open(path, "wb") as handle:
            handle.write(body.encode("utf-8"))
    else:
        path.write_text(body, encoding="utf-8")
    return SimpleNamespace(path=str(path), body=body, **vars(ids))
