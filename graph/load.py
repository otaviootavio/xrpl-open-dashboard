"""Load one month of edges (account or entity level, one asset or all) into integer arrays and igraph.

Vertex ids come from sorting the addresses, so every later step is independent of file order.
"""

from dataclasses import dataclass

import igraph as ig
import numpy as np

from common import ALL, Paths, q


@dataclass(frozen=True)
class MonthGraph:
    month: str
    asset: str              # 'XRP', an asset key, or '*'
    names: np.ndarray       # addresses (or entity ids), sorted; index = vertex id
    src: np.ndarray         # int32
    dst: np.ndarray         # int32
    n: np.ndarray           # int64 transfers per edge
    value: np.ndarray       # float64 in the asset's units; NaN for '*'

    @property
    def weight(self) -> np.ndarray:
        """Value per asset, transfer count for '*'."""
        return self.n.astype(np.float64) if self.asset == ALL else self.value

    def to_igraph(self) -> ig.Graph:
        g = ig.Graph(n=len(self.names), edges=np.column_stack([self.src, self.dst]).tolist(), directed=True)
        g.es["n"] = self.n.tolist()
        g.es["w"] = self.weight.tolist()
        return g


LEVELS = {
    "account": ("transfer_edges_monthly", "from_account", "to_account"),
    "entity": ("graph_entity_edges", "from_entity", "to_entity"),
}


def load_month(con, p: Paths, month: str, asset: str, level: str = "entity", min_transfers: int = 1) -> MonthGraph:
    table, f, t = LEVELS[level]
    where = "true" if asset == ALL else f"asset = '{asset}'"
    value = "NULL::DOUBLE" if asset == ALL else "sum(value_f ORDER BY value_f)"
    con.execute(f"""CREATE OR REPLACE TEMP TABLE lg_e AS
        SELECT {f} AS f, {t} AS t, sum(n_transfers)::BIGINT AS n, {value} AS v
        FROM read_parquet({q(p.part(table, month))}) WHERE {where}
        GROUP BY 1, 2 HAVING sum(n_transfers) >= {int(min_transfers)}""")
    con.execute("""CREATE OR REPLACE TEMP TABLE lg_v AS
        SELECT a, (row_number() OVER (ORDER BY a) - 1)::INTEGER AS id
        FROM (SELECT f AS a FROM lg_e UNION SELECT t FROM lg_e)""")
    names = np.array([r[0] for r in con.execute("SELECT a FROM lg_v ORDER BY id").fetchall()], dtype=object)
    arr = con.execute("""SELECT vf.id AS src, vt.id AS dst, e.n, e.v FROM lg_e e
        JOIN lg_v vf ON vf.a = e.f JOIN lg_v vt ON vt.a = e.t ORDER BY src, dst""").fetchnumpy()
    con.execute("DROP TABLE lg_e; DROP TABLE lg_v")
    v = arr["v"]
    v = np.full(len(arr["n"]), np.nan) if asset == ALL else np.asarray(v, dtype=np.float64)
    return MonthGraph(month, asset, names, np.asarray(arr["src"], np.int32), np.asarray(arr["dst"], np.int32),
                      np.asarray(arr["n"], np.int64), v)
