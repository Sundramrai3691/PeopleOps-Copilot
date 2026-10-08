"""Synthetic paired evaluation tasks and independent Python answer functions.

Only SQLTask specifications enter the production pipeline. Answer functions run
after the pipeline, with no gold SQL or answer rows passed to either model.
"""
from collections import defaultdict
from dataclasses import dataclass
import random
import sqlite3

from .data_agent import DataContract
from .sql_config import SQLTask


SCHEMAS = {
    'inventory': '''CREATE TABLE items(id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE stock(item_id INTEGER, site TEXT, quantity INTEGER);
CREATE TABLE allocations(item_id INTEGER, quantity INTEGER, state TEXT);''',
    'support': '''CREATE TABLE tickets(id INTEGER PRIMARY KEY, team TEXT, closed_day TEXT);
CREATE TABLE events(ticket_id INTEGER, kind TEXT, minutes INTEGER);''',
    'shipping': '''CREATE TABLE parcels(id INTEGER PRIMARY KEY, region TEXT, grams INTEGER);
CREATE TABLE scans(parcel_id INTEGER, status TEXT, day TEXT);''',
}


@dataclass(frozen=True)
class Case:
    case_id: str
    domain: str
    question: str
    columns: tuple[str, ...]

    def task(self, database):
        return SQLTask(self.case_id, self.question, DataContract(self.columns, max_rows=100), database)


CASES = (
    Case('stock_by_item', 'inventory', 'For every item, report its id as item_id and the sum of stock.quantity across all sites as stock_units. Include items without stock as zero. Order by item_id.', ('item_id', 'stock_units')),
    Case('available_units', 'inventory', 'Report one overall available_units value: sum of all stock.quantity minus sum of allocations.quantity whose state is active. Count each stock and allocation row once. Empty sums are zero; do not group by item.', ('available_units',)),
    Case('unallocated_items', 'inventory', 'List item ids as item_id for items with no allocation whose state is active. An item with only cancelled allocations qualifies. Order by item_id.', ('item_id',)),
    Case('east_stock', 'inventory', "For every item report its id as item_id and total stock.quantity at site east as east_units. Include items with no east stock as zero. Order by item_id.", ('item_id', 'east_units')),
    Case('open_by_team', 'support', 'For every team present in tickets, count tickets with closed_day IS NULL as open_tickets. Include teams with no open tickets as zero. Return team and open_tickets, ordered by team.', ('team', 'open_tickets')),
    Case('work_by_team', 'support', "For every team present in tickets, sum events.minutes for events whose kind is work as work_minutes. Each work event counts once, and multiple work events per ticket all count. Include teams with no work as zero. Return team and work_minutes, ordered by team.", ('team', 'work_minutes')),
    Case('unworked_tickets', 'support', "List ticket ids as ticket_id with no event whose kind is work. Tickets with only note events qualify. Order by ticket_id.", ('ticket_id',)),
    Case('march_closed', 'support', 'Count tickets whose closed_day is on or after 2026-03-01 and strictly before 2026-04-01. Return one value named closed_tickets. NULL closed_day is not closed.', ('closed_tickets',)),
    Case('delivered_weight', 'shipping', "Sum parcels.grams once per parcel that has at least one scan whose status is delivered, regardless of date. Duplicate delivered scans must not multiply weight. Return one delivered_grams value; an empty sum is zero.", ('delivered_grams',)),
    Case('delivered_by_region', 'shipping', "For every region present in parcels, count distinct parcels with at least one delivered scan as delivered_parcels. Multiple delivered scans still count one parcel. Include zero-delivery regions. Return region and delivered_parcels, ordered by region.", ('region', 'delivered_parcels')),
    Case('undelivered_ids', 'shipping', "List parcel ids as parcel_id with no scan whose status is delivered. Other scan statuses do not count as delivered. Order by parcel_id.", ('parcel_id',)),
    Case('march_deliveries', 'shipping', "Count distinct parcels with a delivered scan on or after 2026-03-01 and strictly before 2026-04-01. Count a parcel once even with repeated scans in March. Return one march_parcels value.", ('march_parcels',)),
)


def fixture(domain, variant):
    """Deterministic new instances; no claims about unseen model training data."""
    if variant not in (0, 1):
        raise ValueError('unknown variant')
    rng = random.Random(917231 + variant * 101)
    if domain == 'inventory':
        return {
            'items': [(i, f'item-{i}') for i in range(1, 6)],
            'stock': [(1, 'east', rng.randint(20, 50)), (1, 'west', 17),
                      (2, 'east', 12), (2, 'east', 6 + variant), (3, 'west', 9),
                      (4, 'east', 0)],
            'allocations': [(1, 3, 'active'), (1, 4 + variant, 'active'),
                            (2, 5, 'cancelled'), (3, 2, 'active'), (5, 1, 'cancelled')]
                            + ([(2, 2, 'active'), (2, 3, 'active')] if variant else []),
        }
    if domain == 'support':
        return {
            'tickets': [(1, 'alpha', None), (2, 'alpha', '2026-03-01'),
                        (3, 'beta', '2026-04-01'), (4, 'beta', None),
                        (5, 'gamma', '2026-02-28'), (6, 'gamma', '2026-03-31')],
            'events': [(1, 'work', rng.randint(8, 25)), (1, 'work', 7), (1, 'note', 99),
                       (2, 'work', 5), (3, 'note', 100), (4, 'work', 11)]
                       + ([(3, 'work', 13), (3, 'work', 17), (6, 'note', 9)] if variant else []),
        }
    if domain == 'shipping':
        return {
            'parcels': [(1, 'north', rng.randint(100, 900)), (2, 'north', 210),
                        (3, 'south', 350), (4, 'south', 0), (5, 'west', 490)],
            'scans': [(1, 'delivered', '2026-03-01'), (1, 'delivered', '2026-03-02'),
                      (2, 'transit', '2026-03-10'), (3, 'delivered', '2026-04-01'),
                      (4, 'delivered', '2026-02-28'), (5, 'transit', '2026-03-31')]
                      + ([(2, 'delivered', '2026-03-31'), (2, 'delivered', '2026-03-31')] if variant else []),
        }
    raise ValueError('unknown domain')


def create_database(path, domain, data):
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMAS[domain])
        for table, rows in data.items():
            if rows:
                db.executemany(f'INSERT INTO {table} VALUES({",".join("?" for _ in rows[0])})', rows)


def expected(case, data):
    """Calculate answers with integer arithmetic, sets and explicit date bounds."""
    kind = case.case_id
    if case.domain == 'inventory':
        stock = defaultdict(int)
        east = defaultdict(int)
        active = set()
        for item, site, quantity in data['stock']:
            stock[item] += quantity
            if site == 'east':
                east[item] += quantity
        for item, quantity, state in data['allocations']:
            if state == 'active':
                active.add(item)
        ids = sorted(row[0] for row in data['items'])
        if kind == 'stock_by_item':
            rows = [[i, stock[i]] for i in ids]
        elif kind == 'east_stock':
            rows = [[i, east[i]] for i in ids]
        elif kind == 'unallocated_items':
            rows = [[i] for i in ids if i not in active]
        else:
            rows = [[sum(stock.values()) - sum(q for _, q, s in data['allocations'] if s == 'active')]]
    elif case.domain == 'support':
        teams = sorted({t[1] for t in data['tickets']})
        ticket_team = {i: team for i, team, _ in data['tickets']}
        worked = {i for i, k, _ in data['events'] if k == 'work'}
        if kind == 'open_by_team':
            rows = [[team, sum(t == team and d is None for _, t, d in data['tickets'])] for team in teams]
        elif kind == 'work_by_team':
            rows = [[team, sum(m for i, k, m in data['events'] if k == 'work' and ticket_team[i] == team)] for team in teams]
        elif kind == 'unworked_tickets':
            rows = [[i] for i in sorted(ticket_team) if i not in worked]
        else:
            rows = [[sum(d is not None and '2026-03-01' <= d < '2026-04-01' for _, _, d in data['tickets'])]]
    else:
        delivered = {i for i, status, day in data['scans'] if status == 'delivered'}
        if kind == 'delivered_weight':
            rows = [[sum(grams for i, _, grams in data['parcels'] if i in delivered)]]
        elif kind == 'delivered_by_region':
            rows = [[region, sum(r == region and i in delivered for i, r, _ in data['parcels'])]
                    for region in sorted({p[1] for p in data['parcels']})]
        elif kind == 'undelivered_ids':
            rows = [[i] for i, _, _ in sorted(data['parcels']) if i not in delivered]
        else:
            rows = [[len({i for i, status, day in data['scans']
                         if status == 'delivered' and '2026-03-01' <= day < '2026-04-01'})]]
    return {'columns': list(case.columns), 'rows': rows}
