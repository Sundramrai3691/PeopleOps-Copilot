from scripts.evaluation.run_manufacturing_transfer import CASES, expected, fixture


def test_equal_amounts_count_twice_and_null_pass_is_still_a_pass():
    rows={c.case_id:expected(c,fixture(0))['rows'] for c in CASES}
    assert rows['lot_balance']==((1,15),(2,30),(3,9),(4,0),(5,10))
    assert rows['no_pass']==((2,),(4,))
    assert rows['pass_lots']==(('A',1),('B',2),('C',0))
    assert rows['plant_yield']==(('A',15.75),('B',100.0),('C',0.0))
    assert rows['positive_plants']==(('A',15),('B',11))
    assert rows['largest_lots']==(('A',1,30),('A',2,30),('B',3,20),('C',4,0))


def test_second_instance_changes_membership_and_weighted_yield():
    rows={c.case_id:expected(c,fixture(1))['rows'] for c in CASES}
    assert rows['no_pass']==((4,),)
    assert rows['plant_yield']==(('A',18.94),('B',100.0),('C',0.0))
    assert rows['positive_plants']==(('A',19),('B',15))
