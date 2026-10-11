"""E9: community names and one-line summaries computed from gold rows (no model, nothing leaves the host).

Name = the role of the community's highest-PageRank member + the first 8 characters of its entity id,
e.g. "Exchange hub · rLHzPsX6". Summary = size, share of transfers kept inside, and the largest outflow.
"""

ROLE_WORDS = {"issuer": "Issuer", "likely_custodial": "Exchange hub", "amm_pool": "AMM pool", "hub": "Hub",
              "bot_pair": "Bot pair", "unknown": "Hub"}


def role_word(role: str | None) -> str:
    if not role:
        return "Hub"
    return ROLE_WORDS.get(role, role.replace("_", " ").capitalize())


def top_member(members: list[dict]) -> dict | None:
    """Highest PageRank, ties broken by entity id."""
    if not members:
        return None
    return min(members, key=lambda m: (-(m.get("pagerank") or 0.0), m["entity_id"]))


def community_names(members_by_comm: dict[int, list[dict]]) -> dict[int, str]:
    """{community: name}; a name used twice in one view gets " (C<n>)" appended."""
    raw = {}
    for c, members in members_by_comm.items():
        t = top_member(members)
        raw[c] = f"{role_word(t.get('role'))} · {t['entity_id'][:8]}" if t else f"C{c}"
    seen: dict[str, int] = {}
    for nm in raw.values():
        seen[nm] = seen.get(nm, 0) + 1
    return {c: (f"{nm} (C{c})" if seen[nm] > 1 else nm) for c, nm in raw.items()}


def pct(x: float) -> str:
    return f"{round(100 * x)}%"


def community_summary(size: int, internal_n: int | None, out_total: float, flows_out: list[dict] | None,
                      names: dict[int, str]) -> str:
    """'<size> entities · <inside>% of their transfers stay inside · most outflow to <name> (<share>%)'."""
    parts = [f"{int(size):,} entities"]
    if out_total and out_total > 0 and internal_n is not None:
        parts.append(f"{pct(min(internal_n / out_total, 1.0))} of their transfers stay inside")
    flows = [f for f in (flows_out or []) if f.get("n")]
    if flows and out_total and out_total > 0:
        top = max(flows, key=lambda f: (f["n"], -f["to"]))
        parts.append(f"most outflow to {names.get(top['to'], 'C' + str(top['to']))} ({pct(top['n'] / out_total)})")
    return " · ".join(parts)
