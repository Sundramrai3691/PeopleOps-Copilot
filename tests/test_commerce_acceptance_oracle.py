from scripts.evaluation.commerce_acceptance_cases import oracle


def test_hand_calculated_totals_refunds_absent_customers_and_ties():
    data={'customers':[(1,'A'),(2,'B'),(3,'C')],
          'orders':[(1,1,'2026-02-01',3000,'paid'),(2,2,'2026-03-31',3000,'paid'),(3,3,'2026-04-01',9000,'cancelled')],
          'refunds':[(1,1,100,'approved'),(2,1,100,'approved'),(3,1,900,'pending'),(4,2,None,'approved')]}
    expected=[[[6000]],[[1,'2026-02-01',3000],[2,'2026-03-31',3000]],
              [['2026-02',1,3000],['2026-03',1,3000]],[[1,2800],[2,3000],[3,0]],[[3,'C']],[[1,3000],[2,3000]]]
    for case,rows in enumerate(expected):assert oracle(case,data)['rows']==rows
