"""Position-level performance metrics for PostgreSQL trade episodes."""
from collections import defaultdict


def load_postgres_metrics(days: int = 30) -> dict:
    """Load complete position episodes from PostgreSQL and summarize them."""
    from shared.postgres_client import _connection

    try:
        with _connection() as conn:
            rows = conn.execute(
                "SELECT pnl_usdt, result, exit_reason FROM trade_episodes "
                "WHERE closed_at >= now() - (%s * interval '1 day')",
                (max(1, int(days)),),
            ).fetchall()
        return summarize_episodes([
            {'pnl_usdt': row[0], 'result': row[1], 'exit_reason': row[2]}
            for row in rows
        ])
    except Exception:
        return summarize_episodes([])


def summarize_episodes(rows: list[dict]) -> dict:
    """Calculate stable metrics from complete position-level rows."""
    total = len(rows)
    wins = [r for r in rows if float(r.get('pnl_usdt', 0)) > 0]
    losses = [r for r in rows if float(r.get('pnl_usdt', 0)) <= 0]
    gross_profit = sum(float(r.get('pnl_usdt', 0)) for r in wins)
    gross_loss = -sum(float(r.get('pnl_usdt', 0)) for r in losses)
    by_reason = defaultdict(float)
    for row in rows:
        by_reason[str(row.get('exit_reason', ''))] += float(row.get('pnl_usdt', 0))
    return {
        'trades': total,
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': len(wins) / total * 100 if total else 0.0,
        'pnl_usdt': gross_profit - gross_loss,
        'profit_factor': gross_profit / gross_loss if gross_loss else None,
        'avg_win': gross_profit / len(wins) if wins else 0.0,
        'avg_loss': -gross_loss / len(losses) if losses else 0.0,
        'pnl_by_exit_reason': dict(by_reason),
    }
