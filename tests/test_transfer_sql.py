from scripts.evaluation.transfer_sql_cases import CASES, expected, fixture


def test_billing_oracle_handles_duplicate_receipts_credits_and_null_amounts():
    values={c.case_id:expected(c,fixture(0))['rows'] for c in CASES}
    assert values['invoice_balance']==((11,38),(12,100),(13,-70),(14,0),(15,50))
    assert values['settled_by_account']==(('north',50),('south',250),('west',0))
    assert values['receipt_count']==(('north',2),('south',2),('west',0))
    assert values['no_settled_receipt']==((12,),(14,))
    assert values['settled_percentage']==((60.0,),)
    assert values['positive_balances']==((3,),)
    assert values['largest_ties']==(('north',11,100),('north',12,100),('south',13,200),('west',14,0))
    assert values['overdue_by_account']==(('north',1),('south',1),('west',0))


def test_second_instance_changes_absence_and_fraction():
    values={c.case_id:expected(c,fixture(1))['rows'] for c in CASES}
    assert values['no_settled_receipt']==((14,),)
    assert values['settled_percentage']==((80.0,),)
    assert values['invoice_balance']==((11,37),(12,41),(13,-70),(14,0),(15,50))
