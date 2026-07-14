"""Head-to-head: LoL player-level Elo vs team-name Elo, walk-forward, on the
IDENTICAL prediction set (same games, same experience gating) — so the Brier
difference is purely the model, not the sample.

Prints both Briers across a small K sweep for the player model (its best K is
unknown a priori) and a per-bucket table for the winner. The verdict decides
what the live bot uses for LoL (see OPERATOR.md).

Usage: python compare_lol_models.py
"""

import sys

from elo import lol_players


def brier(preds):
    return sum((p - a) ** 2 for p, a in preds) / len(preds)


def main():
    import glob
    if glob.glob("data/oe/*.csv"):
        games = lol_players.load_oe_games()  # Oracle's Elixir bulk CSVs (preferred)
    else:
        games = lol_players.rows_to_games(lol_players.fetch_player_rows())  # Leaguepedia fallback
    print(f"games: {len(games)}")
    if len(games) < 2000:
        print("Not enough games for a trustworthy verdict — need more backfill.")
        sys.exit(1)

    team_preds = lol_players.replay_team(games, k=72.0)  # tuned team K
    print(f"\nTEAM Elo   (k=72): {len(team_preds)} preds | Brier {brier(team_preds):.4f}")

    best = (None, None, None)
    for k in (16.0, 24.0, 32.0, 48.0):
        _, preds = lol_players.replay_players(games, k=k)
        b = brier(preds)
        print(f"PLAYER Elo (k={k:>4}): {len(preds)} preds | Brier {b:.4f}")
        if best[0] is None or b < best[0]:
            best = (b, k, preds)

    tb = brier(team_preds)
    print(f"\nVERDICT: player best {best[0]:.4f} (k={best[1]}) vs team {tb:.4f} "
          f"-> {'PLAYER wins' if best[0] < tb - 1e-4 else 'TEAM wins (or tie) — keep team model'}")

    # calibration table for the winner
    preds = best[2] if best[0] < tb - 1e-4 else team_preds
    print(f"\n{'bucket':>10} {'n':>6} {'predicted':>10} {'actual':>8}")
    for i in range(10):
        lo, hi = i / 10, (i + 1) / 10
        b_ = [(p, a) for p, a in preds if lo <= p < hi or (i == 9 and p == 1.0)]
        if not b_:
            continue
        print(f"  {lo:>4.0%}-{hi:<4.0%} {len(b_):>6} {sum(p for p,_ in b_)/len(b_):>9.1%} "
              f"{sum(a for _,a in b_)/len(b_):>7.1%}")


if __name__ == "__main__":
    main()
