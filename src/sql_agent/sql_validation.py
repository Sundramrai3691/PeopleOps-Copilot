"""Shared SQL statement classification used by workflow and database backends."""

from sqlglot import exp, parse


READ_QUERY_TYPES = (exp.Select, exp.Union, exp.Intersect, exp.Except)


def parse_statement(sql: str, dialect: str):
    nodes = parse(sql, read=dialect)
    if len(nodes) != 1:
        raise ValueError("exactly one SQL statement required")
    return nodes[0]


def is_read_query(node) -> bool:
    return isinstance(node, READ_QUERY_TYPES)


def parse_read_query(sql: str, dialect: str):
    node = parse_statement(sql, dialect)
    if not is_read_query(node):
        raise ValueError("one read-only query required")
    return node
